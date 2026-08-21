/*
 * Implement report card: the fix-round "This Round" block (G3).
 *
 * `state_machine._transition_to_fix` re-uses the SAME implement Step object for
 * every fix iteration, and each round appends its own `step_completed` record
 * to that step's jsonl. Everything the card showed was cumulative by
 * construction — `outputs.files_changed` is re-derived from a flow-baseline git
 * diff after every round, and `outputs.token_usage` is published as the carried
 * total — so a reader of round 3's card had no way to see what round 3 actually
 * did.
 *
 * These checks pin the fix, purely in the numbers/DOM (no LLM call was added):
 *   - the pure round arithmetic (fix-iteration detection, file-list
 *     normalization, the new-key-first / set-difference / empty / unavailable
 *     resolution, and the predecessor-record walk with its duplicate-delivery
 *     de-dup);
 *   - the rendered block itself (present only for a fix round, new key wins,
 *     set-difference fallback, the empty-difference copy);
 *   - the cumulative labelling of the status bar and the usage footnote, and
 *     that a NON-fix card is left exactly as it was.
 *
 * Loaded by tests/frontend/test_app_pure.mjs (which supplies the shared DOM
 * stub) and pulled into the pytest run through
 * tests/test_frontend_implement_fix_round.py.
 */
import assert from "node:assert/strict";

export function registerImplementFixRoundTests(ctx) {
  const { app, check, findOne, findAll } = ctx;

  function texts(frag, cls) {
    return findAll(frag, cls).map((n) => n.textContent);
  }
  function sectionTitles(frag) {
    return texts(frag, "step-report__section-title");
  }
  function fileRows(node) {
    return texts(node, "step-report__file-row");
  }
  // One `step_completed` history record for the implement step, in the exact
  // envelope shape the daemon pushes (`data.step` = the full step snapshot).
  function completedRecord(ordinal, inputs, outputs, over) {
    return Object.assign({
      step_id: "implement_1",
      step_type: "implement",
      ordinal,
      type: "step_completed",
      timestamp: "2026-08-21T10:0" + ordinal + ":00",
      data: {
        step: {
          step_id: "implement_1",
          step_type: "implement",
          status: "completed",
          inputs,
          outputs,
        },
      },
    }, over || {});
  }
  function implementStep(inputs, outputs) {
    return {
      step_type: "implement",
      step_id: "implement_1",
      status: "completed",
      inputs: inputs || {},
      outputs: outputs || {},
    };
  }

  // =========================================================================
  // implementFixIteration — the ONLY signal that a card is a fix round
  // =========================================================================
  check("fix iteration: round one (no fix inputs) is 0", () => {
    assert.equal(app.implementFixIteration(implementStep({}, {})), 0);
    assert.equal(app.implementFixIteration(implementStep({ fix_iteration: 0 }, {})), 0);
  });

  check("fix iteration: the 1-based counter is read straight off inputs", () => {
    assert.equal(app.implementFixIteration(implementStep({ fix_iteration: 3 }, {})), 3);
    assert.equal(app.implementFixIteration(implementStep({ fix_iteration: "2" }, {})), 2);
  });

  check("fix iteration: the boolean alone still counts as a fix round", () => {
    assert.equal(
      app.implementFixIteration(implementStep({ is_fix_iteration: true }, {})), 1);
  });

  check("fix iteration: a non-implement step is never a fix round", () => {
    assert.equal(app.implementFixIteration({
      step_type: "self_check", inputs: { fix_iteration: 4 },
    }), 0);
    assert.equal(app.implementFixIteration(null), 0);
    assert.equal(app.implementFixIteration({ step_type: "implement" }), 0);
  });

  // =========================================================================
  // normalizeFileList — both sides of the set difference must agree
  // =========================================================================
  check("file list: dedupes, trims and normalizes separators", () => {
    assert.deepEqual(
      app.normalizeFileList(["a/b.py", "a\\b.py", "  c.py  ", "", null, "c.py"]),
      ["a/b.py", "c.py"]);
  });

  check("file list: a non-array is an empty list, never a throw", () => {
    assert.deepEqual(app.normalizeFileList(undefined), []);
    assert.deepEqual(app.normalizeFileList("a.py"), []);
  });

  // =========================================================================
  // fixRoundChangedFiles — new key first, set difference as the old-history fallback
  // =========================================================================
  check("round files: the engine's own fix_round_files_changed wins over the diff", () => {
    const got = app.fixRoundChangedFiles(
      {
        fix_round_files_changed: ["src/b.py"],
        files_changed: ["src/a.py", "src/b.py", "src/c.py"],
      },
      ["src/a.py"],
    );
    assert.equal(got.source, "reported");
    assert.deepEqual(got.files, ["src/b.py"]);
  });

  check("round files: without the new key, the predecessor's list is differenced out", () => {
    const got = app.fixRoundChangedFiles(
      { files_changed: ["src/a.py", "src/b.py", "src/c.py"] },
      ["src/a.py", "src/c.py"],
    );
    assert.equal(got.source, "diff");
    assert.deepEqual(got.files, ["src/b.py"]);
  });

  check("round files: an EMPTY difference is a real answer, not a missing one", () => {
    const got = app.fixRoundChangedFiles(
      { files_changed: ["src/a.py"] },
      ["src/a.py"],
    );
    assert.equal(got.source, "empty");
    assert.deepEqual(got.files, []);
  });

  check("round files: no predecessor at all is 'unavailable', distinct from empty", () => {
    const got = app.fixRoundChangedFiles({ files_changed: ["src/a.py"] }, null);
    assert.equal(got.source, "unavailable");
    assert.deepEqual(got.files, []);
  });

  check("round files: an empty new key falls through to the difference", () => {
    const got = app.fixRoundChangedFiles(
      { fix_round_files_changed: [], files_changed: ["src/a.py", "src/b.py"] },
      ["src/a.py"],
    );
    assert.equal(got.source, "diff");
    assert.deepEqual(got.files, ["src/b.py"]);
  });

  // =========================================================================
  // accumulatePriorFilesChangedByStep — locating the previous round's record
  // =========================================================================
  check("predecessor: only step_completed records are probed", () => {
    assert.equal(app.isStepCompletedRecord(completedRecord(0, {}, {})), true);
    assert.equal(app.isStepCompletedRecord({ type: "step_output" }), false);
    assert.equal(app.isStepCompletedRecord({ message: { role: "assistant" } }), false);
    assert.equal(app.isStepCompletedRecord(null), false);
  });

  check("predecessor: each round sees the PREVIOUS round's cumulative list", () => {
    const records = [
      completedRecord(0, {}, { files_changed: ["a.py"] }),
      completedRecord(1, { fix_iteration: 1 }, { files_changed: ["a.py", "b.py"] }),
      completedRecord(2, { fix_iteration: 2 }, { files_changed: ["a.py", "b.py", "c.py"] }),
    ];
    const prior = app.accumulatePriorFilesChangedByStep(records);
    assert.equal(prior[0], null, "the first completion has no predecessor");
    assert.deepEqual(prior[1], ["a.py"]);
    assert.deepEqual(prior[2], ["a.py", "b.py"]);
  });

  check("predecessor: chat records between the rounds do not shift the walk", () => {
    const records = [
      completedRecord(0, {}, { files_changed: ["a.py"] }),
      { step_id: "implement_1", ordinal: 1, message: { role: "assistant", content: "hi" } },
      completedRecord(2, { fix_iteration: 1 }, { files_changed: ["a.py", "b.py"] }),
    ];
    const prior = app.accumulatePriorFilesChangedByStep(records);
    assert.equal(prior[1], null, "a chat record carries no predecessor slot");
    assert.deepEqual(prior[2], ["a.py"]);
  });

  check("predecessor: two steps' rounds stay independent", () => {
    const records = [
      completedRecord(0, {}, { files_changed: ["a.py"] }),
      completedRecord(0, {}, { files_changed: ["z.py"] },
        { step_id: "implement_2", data: {
          step: { step_id: "implement_2", step_type: "implement", inputs: {},
            outputs: { files_changed: ["z.py"] } } } }),
      completedRecord(1, { fix_iteration: 1 }, { files_changed: ["a.py", "b.py"] }),
    ];
    const prior = app.accumulatePriorFilesChangedByStep(records);
    assert.equal(prior[1], null, "implement_2's first completion has no predecessor");
    assert.deepEqual(prior[2], ["a.py"], "implement_1's round 2 must not see implement_2");
  });

  check("predecessor: a re-delivered record resolves to the SAME predecessor, not itself", () => {
    const first = completedRecord(0, {}, { files_changed: ["a.py"] });
    const second = completedRecord(1, { fix_iteration: 1 },
      { files_changed: ["a.py", "b.py"] });
    // The REST snapshot and the websocket append overlap: the same
    // stepId#ordinal arrives twice in one ordered array.
    const prior = app.accumulatePriorFilesChangedByStep([first, second, second]);
    assert.deepEqual(prior[1], ["a.py"]);
    assert.deepEqual(prior[2], ["a.py"],
      "the duplicate must not take its own first delivery as its predecessor");
  });

  check("predecessor: an empty / malformed array never throws", () => {
    assert.deepEqual(app.accumulatePriorFilesChangedByStep([]), []);
    assert.deepEqual(app.accumulatePriorFilesChangedByStep(null), []);
    assert.deepEqual(app.accumulatePriorFilesChangedByStep([null, 7]), [null, null]);
  });

  // =========================================================================
  // The rendered block
  // =========================================================================
  const CUMULATIVE_OUTPUTS = {
    completion_status: "complete",
    summary: "Round 2: reworked the parser",
    files_changed: ["src/a.py", "src/b.py", "tests/test_a.py"],
    tests_added: ["tests/test_a.py"],
  };

  check("round one renders NO 'This Round' block and NO cumulative marker", () => {
    const frag = app.renderImplementReport(
      implementStep({}, CUMULATIVE_OUTPUTS), CUMULATIVE_OUTPUTS, null);
    assert.ok(!sectionTitles(frag).some((t) => t.includes("This Round")),
      `round one must not gain a fix-round block: ${sectionTitles(frag).join(" | ")}`);
    assert.ok(!frag.textContent.includes("cumulative"),
      "round one's status bar must be byte-identical to before");
    // …and the cumulative body is untouched.
    assert.ok(sectionTitles(frag).some((t) => t.startsWith("Files Changed (3)")));
  });

  check("a fix round renders the block ABOVE the cumulative body, with this round's summary", () => {
    const outputs = Object.assign({}, CUMULATIVE_OUTPUTS, {
      fix_round_files_changed: ["src/b.py"],
    });
    const frag = app.renderImplementReport(
      implementStep({ is_fix_iteration: true, fix_iteration: 2 }, outputs),
      outputs, null);
    const titles = sectionTitles(frag);
    assert.equal(titles[0], "This Round (fix iteration 2)",
      `the fix-round block must come first: ${titles.join(" | ")}`);
    const block = findAll(frag, "step-report__section")[0];
    assert.ok(block.textContent.includes("Round 2: reworked the parser"),
      "this round's own summary belongs in the block");
    assert.deepEqual(fileRows(block), ["src/b.py"],
      "the block lists only the round's files, not the cumulative set");
    // The cumulative body survives in full below it.
    assert.ok(titles.some((t) => t.startsWith("Files Changed (3)")));
    assert.deepEqual(fileRows(findAll(frag, "step-report__section")
      .find((s) => s.textContent.startsWith("Files Changed"))),
    ["src/a.py", "src/b.py", "tests/test_a.py"]);
  });

  // `_run_single_llm_call` writes THIS round's text to `outputs.summary` and
  // derives `group_summaries` from that same string, so both feed the block
  // above — the body below must not print it a second time as cumulative.
  function countOccurrences(haystack, needle) {
    let n = 0;
    for (let i = haystack.indexOf(needle); i !== -1; i = haystack.indexOf(needle, i + 1)) n++;
    return n;
  }

  check("a fix round shows this round's summary once, not again under 'Summary'", () => {
    const outputs = Object.assign({}, CUMULATIVE_OUTPUTS, {
      group_summaries: [{ group_id: "G1", summary: "Round 2: reworked the parser" }],
    });
    const frag = app.renderImplementReport(
      implementStep({ is_fix_iteration: true, fix_iteration: 2 }, outputs),
      outputs, null);
    const titles = sectionTitles(frag);
    assert.ok(!titles.includes("Summary"),
      `the cumulative body must not repeat the round summary: ${titles.join(" | ")}`);
    assert.equal(countOccurrences(frag.textContent, "Round 2: reworked the parser"), 1,
      "this round's summary belongs on the card exactly once");
  });

  check("a fix round with no group_summaries also drops the duplicate 'Summary'", () => {
    const frag = app.renderImplementReport(
      implementStep({ fix_iteration: 2 }, CUMULATIVE_OUTPUTS),
      CUMULATIVE_OUTPUTS, null);
    assert.ok(!sectionTitles(frag).includes("Summary"));
    assert.equal(countOccurrences(frag.textContent, "Round 2: reworked the parser"), 1);
  });

  check("a fix round KEEPS group summaries that differ from this round's text", () => {
    const outputs = Object.assign({}, CUMULATIVE_OUTPUTS, {
      group_summaries: [
        { group_id: "G1", summary: "Round 2: reworked the parser" },
        { group_id: "G2", summary: "Earlier: added the lexer" },
      ],
    });
    const frag = app.renderImplementReport(
      implementStep({ fix_iteration: 2 }, outputs), outputs, null);
    const section = findAll(frag, "step-report__section")
      .find((s) => s.textContent.startsWith("Summary"));
    assert.ok(section, "a genuinely cumulative group summary must survive");
    assert.ok(section.textContent.includes("Earlier: added the lexer"));
    assert.equal(countOccurrences(frag.textContent, "Round 2: reworked the parser"), 1,
      "the round's own entry is still shown only in the block above");
  });

  check("round one keeps its Summary section untouched", () => {
    const outputs = Object.assign({}, CUMULATIVE_OUTPUTS, {
      group_summaries: [{ group_id: "G1", summary: "Round 2: reworked the parser" }],
    });
    const frag = app.renderImplementReport(
      implementStep({}, outputs), outputs, null);
    const section = findAll(frag, "step-report__section")
      .find((s) => s.textContent.startsWith("Summary"));
    assert.ok(section, "a non-fix card must be byte-identical to before");
    assert.ok(section.textContent.includes("Round 2: reworked the parser"));
  });

  check("a fix round's status bar is labelled cumulative, with the iteration count", () => {
    const frag = app.renderImplementReport(
      implementStep({ fix_iteration: 3 }, CUMULATIVE_OUTPUTS),
      CUMULATIVE_OUTPUTS, null);
    const bar = findOne(frag, "step-report__status-bar");
    assert.ok(bar.textContent.includes("cumulative · incl. 3 fix iteration(s)"),
      `status bar was: ${bar.textContent}`);
    assert.ok(bar.textContent.includes("3 files"),
      "the cumulative counts themselves are unchanged");
  });

  check("old history without the new key falls back to the set difference", () => {
    const frag = app.renderImplementReport(
      implementStep({ fix_iteration: 1 }, CUMULATIVE_OUTPUTS),
      CUMULATIVE_OUTPUTS,
      { priorFilesChanged: ["src/a.py"] });
    const block = findAll(frag, "step-report__section")[0];
    assert.equal(block.textContent.startsWith("This Round (fix iteration 1)"), true);
    assert.deepEqual(fileRows(block), ["src/b.py", "tests/test_a.py"]);
    assert.ok(block.textContent.includes("Changed this round (2)"));
  });

  check("an empty difference says so instead of showing an empty list", () => {
    const frag = app.renderImplementReport(
      implementStep({ fix_iteration: 2 }, CUMULATIVE_OUTPUTS),
      CUMULATIVE_OUTPUTS,
      { priorFilesChanged: CUMULATIVE_OUTPUTS.files_changed });
    const block = findAll(frag, "step-report__section")[0];
    assert.deepEqual(fileRows(block), []);
    assert.ok(block.textContent.includes(
      "No new files this round (existing files may have been edited again)"),
    `block was: ${block.textContent}`);
  });

  check("no new key and no predecessor reads as 'not recorded', not as 'no changes'", () => {
    const frag = app.renderImplementReport(
      implementStep({ fix_iteration: 2 }, CUMULATIVE_OUTPUTS),
      CUMULATIVE_OUTPUTS, null);
    const block = findAll(frag, "step-report__section")[0];
    assert.ok(block.textContent.includes("This round's file list was not recorded"),
      `block was: ${block.textContent}`);
    assert.ok(!block.textContent.includes("No new files this round"));
  });

  check("the block reuses the card's own file primitives — no new visual vocabulary", () => {
    const outputs = Object.assign({}, CUMULATIVE_OUTPUTS, {
      fix_round_files_changed: ["src/b.py", "tests/test_a.py"],
    });
    const frag = app.renderImplementReport(
      implementStep({ fix_iteration: 1 }, outputs), outputs, null);
    const block = findAll(frag, "step-report__section")[0];
    assert.ok(findOne(block, "step-report__files"), "step-report__files wrapper");
    assert.equal(findAll(block, "step-report__file-group").length, 2,
      "one group per top-level directory, exactly as the cumulative list renders");
    assert.deepEqual(texts(block, "step-report__file-dir"), ["src/ (1)", "tests/ (1)"]);
  });

  check("renderChangedFileTree is the ONE builder both lists go through", () => {
    const wrap = app.renderChangedFileTree(["b/z.py", "a/y.py", "top.md"]);
    assert.deepEqual(texts(wrap, "step-report__file-dir"),
      ["a/ (1)", "b/ (1)", "./ (1)"], "root-level files sort last, as before");
    assert.deepEqual(fileRows(wrap), ["a/y.py", "b/z.py", "top.md"]);
  });

  // =========================================================================
  // Usage footnote — the number is carried, so the card must say so
  // =========================================================================
  const USAGE = { token_usage: { input_tokens: 100, output_tokens: 40 } };

  check("usage footnote: a fix round's card labels the carried total as cumulative", () => {
    const outputs = Object.assign({}, CUMULATIVE_OUTPUTS, USAGE);
    const card = app.renderStepReport(
      implementStep({ fix_iteration: 2 }, outputs), null);
    const foot = findOne(card, "step-report__usage");
    assert.ok(foot, "the usage footnote still renders");
    assert.ok(foot.textContent.includes("(cumulative)"),
      `footnote was: ${foot.textContent}`);
  });

  check("usage footnote: round one's footnote is left exactly as it was", () => {
    const outputs = Object.assign({}, CUMULATIVE_OUTPUTS, USAGE);
    const card = app.renderStepReport(implementStep({}, outputs), null);
    const foot = findOne(card, "step-report__usage");
    assert.ok(foot);
    assert.ok(!foot.textContent.includes("cumulative"),
      `non-fix footnote must not change: ${foot.textContent}`);
  });

  check("usage footnote: a non-implement step is never annotated", () => {
    const card = app.renderStepReport({
      step_type: "self_check",
      step_id: "self_check_1",
      status: "completed",
      inputs: { fix_iteration: 2 },
      outputs: Object.assign({ issues: [] }, USAGE),
    }, null);
    const foot = findOne(card, "step-report__usage");
    assert.ok(foot && !foot.textContent.includes("cumulative"));
  });

  // =========================================================================
  // End to end through the record path
  // =========================================================================
  check("record path: normalizeRecord carries the step snapshot's inputs", () => {
    const norm = app.normalizeRecord(
      completedRecord(1, { fix_iteration: 2, is_fix_iteration: true },
        { files_changed: ["a.py"] }));
    assert.equal(norm.kind, "step_completed");
    assert.equal(norm.stepReport.inputs.fix_iteration, 2);
  });

  check("record path: a fix-round record renders the block with its diffed files", () => {
    const norm = app.normalizeRecord(
      completedRecord(1, { fix_iteration: 1 }, CUMULATIVE_OUTPUTS));
    norm.priorFilesChanged = ["src/a.py"];
    const row = app.renderConversationRecord(norm);
    const block = findAll(row, "step-report__section")[0];
    assert.equal(block.textContent.startsWith("This Round (fix iteration 1)"), true);
    assert.deepEqual(fileRows(block), ["src/b.py", "tests/test_a.py"]);
  });

  check("record path: round one's record renders no block at all", () => {
    const norm = app.normalizeRecord(completedRecord(0, {}, CUMULATIVE_OUTPUTS));
    norm.priorFilesChanged = null;
    const row = app.renderConversationRecord(norm);
    assert.ok(!row.textContent.includes("This Round"),
      "no fix-round block on the first completion");
  });
}
