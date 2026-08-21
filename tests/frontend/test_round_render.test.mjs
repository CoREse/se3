/*
 * Per-round test-result rendering in the chat stream (G2).
 *
 * The test step writes one synthetic history record per fix round
 * (`test.py:_record_test_history` → `record_response`). Its payload —
 * `{overall_passed, phases: [{name, passed, returncode, stdout_tail,
 * stderr_tail}]}` — is not a Claude stream line, so the record's `content`
 * extracts to "" and the console used to render it as
 * "(no readable content for this record)". A FAILED round is worse off still:
 * it returns REVISION_NEEDED, which is non-terminal, so no `step_completed`
 * card is ever emitted for it — the rounds a reader most needs were exactly the
 * ones with nothing to show.
 *
 * These checks pin the fix: the pure recognizer (positive AND negative cases),
 * the pure failure-summary extractor across several runner tail shapes plus its
 * fallback, the streaming card itself, and the terminal `renderTestReport`
 * card's reuse of the SAME phase builder — one helper, two call sites.
 *
 * Loaded by tests/frontend/test_app_pure.mjs (which supplies the shared DOM
 * stub), and pulled into the pytest run through
 * tests/test_frontend_test_round_render.py.
 */
import assert from "node:assert/strict";

// A pytest tail with the short-summary block and the closing banner.
const PYTEST_TAIL = [
  "tests/test_alpha.py::test_one PASSED                                  [ 50%]",
  "tests/test_alpha.py::test_two FAILED                                  [100%]",
  "",
  "=========================== short test summary info ===========================",
  "FAILED tests/test_alpha.py::test_two - AssertionError: assert 1 == 2",
  "ERROR tests/test_beta.py::test_three - ImportError: no module named zzz",
  "========================= 1 failed, 1 passed in 0.42s ==========================",
].join("\n");

export function registerTestRoundRenderTests(ctx) {
  const { app, check, findOne, findAll } = ctx;

  function phaseRecord(payload, over) {
    return app.normalizeRecord(Object.assign({
      step_id: "test_1",
      step_type: "test",
      message: {
        role: "assistant",
        content: "",
        raw_json: [payload],
        timestamp: "2026-08-21T10:00:00",
      },
    }, over || {}));
  }
  const texts = (frag, cls) => findAll(frag, cls).map((n) => n.textContent);

  // =========================================================================
  // The pure recognizer
  // =========================================================================
  check("test round: a synthetic test payload is recognized on an empty assistant record", () => {
    const payload = { overall_passed: false, phases: [{ name: "unit", passed: false }] };
    const got = app.testRoundResultPayload(phaseRecord(payload));
    assert.ok(got, "the synthetic payload must be recognized");
    assert.equal(got.overall_passed, false);
    assert.equal(got.phases[0].name, "unit");
  });

  check("test round: an ordinary empty assistant record is NOT mistaken for one", () => {
    // The exact shape that used to (and still must) reach "(no readable content)".
    assert.equal(app.testRoundResultPayload(app.normalizeRecord({
      step_id: "impl_1",
      step_type: "implement",
      message: { role: "assistant", content: "", raw_json: [] },
    })), null);
    assert.equal(app.testRoundResultPayload(app.normalizeRecord({
      message: { role: "assistant", content: "" },
    })), null);
  });

  check("test round: a record with a readable body is never recognized", () => {
    const norm = phaseRecord({ overall_passed: true, phases: [] });
    norm.content = "All good.";
    assert.equal(app.testRoundResultPayload(norm), null);
  });

  check("test round: role, partial flag and payload shape are all required", () => {
    const payload = { overall_passed: true, phases: [] };
    const user = phaseRecord(payload);
    user.role = "user";
    assert.equal(app.testRoundResultPayload(user), null, "user role is not a test round");

    const partial = phaseRecord(payload);
    partial.partial = true;
    assert.equal(app.testRoundResultPayload(partial), null,
      "an in-flight stream fragment is not a settled round");

    // phases without overall_passed, and overall_passed without phases.
    assert.equal(app.testRoundResultPayload(
      phaseRecord({ phases: [{ name: "unit", passed: true }] })), null);
    assert.equal(app.testRoundResultPayload(
      phaseRecord({ overall_passed: true })), null);
    // A non-boolean overall_passed is some other payload, not a test round.
    assert.equal(app.testRoundResultPayload(
      phaseRecord({ overall_passed: "yes", phases: [] })), null);
    assert.equal(app.testRoundResultPayload(null), null);
    assert.equal(app.testRoundResultPayload({}), null);
  });

  // =========================================================================
  // The pure failure-summary extractor
  // =========================================================================
  check("failure summary: pytest tail yields the banner first, then the failure rows", () => {
    const lines = app.extractTestFailureSummary(PYTEST_TAIL);
    assert.equal(lines[0], "========================= 1 failed, 1 passed in 0.42s ==========================",
      `banner must lead the summary, got: ${JSON.stringify(lines)}`);
    assert.ok(lines.some((l) => l.startsWith("FAILED tests/test_alpha.py::test_two")));
    assert.ok(lines.some((l) => l.startsWith("ERROR tests/test_beta.py::test_three")));
    // The per-test progress rows are noise — they stay in the folded full tail.
    assert.ok(!lines.some((l) => l.includes("[ 50%]")),
      `progress rows must not be part of the headline: ${JSON.stringify(lines)}`);
  });

  check("failure summary: unittest / go / jest / cargo verdict lines are recognized", () => {
    for (const [tail, needle] of [
      ["Ran 12 tests in 3.1s\n\nFAILED (failures=1, errors=0)", "FAILED (failures=1, errors=0)"],
      ["--- FAIL: TestThing (0.00s)\n    thing_test.go:14: boom\nFAIL", "--- FAIL: TestThing (0.00s)"],
      ["Test Suites: 1 failed, 2 passed\nTests:       3 failed, 9 passed", "Tests:       3 failed, 9 passed"],
      ["running 4 tests\ntest result: FAILED. 1 passed; 3 failed", "test result: FAILED. 1 passed; 3 failed"],
    ]) {
      const lines = app.extractTestFailureSummary(tail);
      assert.ok(lines.includes(needle),
        `expected ${JSON.stringify(needle)} in ${JSON.stringify(lines)}`);
    }
  });

  check("failure summary: an unrecognized tail falls back to its last non-empty lines", () => {
    const tail = [
      "Traceback (most recent call last):",
      '  File "run.py", line 3, in <module>',
      "    main()",
      "",
      "ZeroDivisionError: division by zero",
      "",
    ].join("\n");
    const lines = app.extractTestFailureSummary(tail);
    assert.equal(lines.length, 3, `fallback keeps a bounded tail: ${JSON.stringify(lines)}`);
    assert.equal(lines[lines.length - 1], "ZeroDivisionError: division by zero",
      "the traceback's final message is the useful part and must survive");
  });

  check("failure summary: empty / absent tails yield nothing at all", () => {
    assert.deepEqual(app.extractTestFailureSummary(""), []);
    assert.deepEqual(app.extractTestFailureSummary("   \n\n  \n"), []);
    assert.deepEqual(app.extractTestFailureSummary(null), []);
    assert.deepEqual(app.extractTestFailureSummary(undefined), []);
  });

  check("failure summary: the line cap is honoured and repeats are collapsed", () => {
    const many = Array.from({ length: 20 },
      (_, i) => `FAILED tests/test_x.py::test_${i}`).join("\n");
    assert.equal(app.extractTestFailureSummary(many).length, 6, "default cap is 6 lines");
    assert.equal(app.extractTestFailureSummary(many, 2).length, 2);
    const dup = "FAILED tests/test_x.py::test_one\nFAILED tests/test_x.py::test_one";
    assert.deepEqual(app.extractTestFailureSummary(dup), ["FAILED tests/test_x.py::test_one"]);
  });

  check("failure summary: only the LAST banner in a multi-section tail is taken", () => {
    const tail = [
      "======================== 5 passed in 0.10s =========================",
      "======================== 2 failed, 3 passed in 0.20s =========================",
    ].join("\n");
    assert.equal(app.extractTestFailureSummary(tail)[0],
      "======================== 2 failed, 3 passed in 0.20s =========================");
  });

  // =========================================================================
  // Which output field the summary is read from
  // =========================================================================
  check("phase output: the _tail spelling wins, plain stdout/stderr is the fallback", () => {
    assert.deepEqual(
      app.testPhaseOutputTails({ stdout_tail: "tail", stdout: "full", stderr_tail: "etail" }),
      { stdout: "tail", stderr: "etail" });
    // step.outputs.test_results keeps a FAILED phase's capture unsuffixed.
    assert.deepEqual(
      app.testPhaseOutputTails({ stdout: "full", stderr: "err" }),
      { stdout: "full", stderr: "err" });
    assert.deepEqual(app.testPhaseOutputTails({}), { stdout: "", stderr: "" });
  });

  check("phase output: a phase with no captured output renders no failure block", () => {
    assert.equal(app.renderTestPhaseFailureDetail({ name: "unit", passed: false }), null);
    assert.equal(app.renderTestPhaseFailureDetail(null), null);
  });

  // =========================================================================
  // The streaming per-round card
  // =========================================================================
  check("round card: a passing round renders a PASSED bar and one ✓ per phase", () => {
    const card = app.renderTestRoundCard(phaseRecord({
      overall_passed: true,
      phases: [
        { name: "unit", passed: true, returncode: 0 },
        { name: "lint", passed: true, returncode: 0 },
      ],
    }));
    assert.ok(card, "a recognized round must produce a card");
    // Same shell + primitives as the terminal cards.
    assert.ok(card.classList.contains("step-report"));
    assert.ok(card.classList.contains("kind-test"));
    assert.equal(findOne(card, "step-report__title").textContent, "Test Run Result");
    const label = findOne(card, "step-report__label");
    assert.ok(label.classList.contains("ok"));
    assert.equal(label.textContent, "PASSED");
    assert.ok(card.textContent.includes("2 / 2 phases"));
    assert.deepEqual(texts(card, "step-report__icon"), ["✓", "✓"]);
    assert.equal(findAll(card, "step-report__icon").filter(
      (n) => n.classList.contains("fail")).length, 0);
  });

  check("round card: a failing round renders FAILED, the exit code and the failure headline", () => {
    const card = app.renderTestRoundCard(phaseRecord({
      overall_passed: false,
      phases: [
        { name: "lint", passed: true, returncode: 0 },
        { name: "unit", passed: false, returncode: 1, stdout_tail: PYTEST_TAIL, stderr_tail: "" },
      ],
    }));
    const label = findOne(card, "step-report__label");
    assert.ok(label.classList.contains("fail"));
    assert.equal(label.textContent, "FAILED");
    assert.ok(card.textContent.includes("1 / 2 phases"));
    assert.deepEqual(texts(card, "step-report__icon"), ["✓", "✗"]);
    // Exit code sits beside the failed phase name as muted secondary text.
    assert.ok(texts(card, "step-report__muted").some((t) => t.includes("exit 1")),
      `exit code must be shown: ${JSON.stringify(texts(card, "step-report__muted"))}`);
    // The default-visible headline, extracted from the tail.
    const sections = texts(card, "step-report__section-title");
    assert.ok(sections.includes("Failure summary"), JSON.stringify(sections));
    assert.ok(sections.includes("stdout (tail)"), JSON.stringify(sections));
    assert.ok(!sections.includes("stderr (tail)"),
      "an empty stderr tail must not render an empty section");
    assert.ok(card.textContent.includes("1 failed, 1 passed in 0.42s"));
    assert.ok(card.textContent.includes("FAILED tests/test_alpha.py::test_two"));
  });

  check("round card: a passing phase never renders a failure block", () => {
    const card = app.renderTestRoundCard(phaseRecord({
      overall_passed: true,
      phases: [{ name: "unit", passed: true, returncode: 0, stdout_tail: PYTEST_TAIL }],
    }));
    assert.ok(!texts(card, "step-report__section-title").includes("Failure summary"));
  });

  check("round card: a long tail folds, and the headline stays outside the fold", () => {
    const long = PYTEST_TAIL + "\n" + "noise line filler ".repeat(200);
    const card = app.renderTestRoundCard(phaseRecord({
      overall_passed: false,
      phases: [{ name: "unit", passed: false, returncode: 2, stdout_tail: long }],
    }));
    const fold = findOne(card, "foldable");
    assert.ok(fold, "a tail past the fold threshold must be collapsed by default");
    assert.ok(fold.classList.contains("folded"));
    // The extracted headline is rendered as a plain list, not inside the fold.
    const list = findOne(card, "step-report__list");
    assert.ok(list.textContent.includes("1 failed, 1 passed in 0.42s"),
      "the summary must be readable without expanding anything");
  });

  check("round card: an unrecognized record yields no card at all", () => {
    assert.equal(app.renderTestRoundCard(app.normalizeRecord({
      message: { role: "assistant", content: "" },
    })), null);
  });

  // =========================================================================
  // Streaming position: the conversation record itself
  // =========================================================================
  check("stream: a synthetic test record renders the card instead of the empty state", () => {
    const row = app.renderConversationRecord(phaseRecord({
      overall_passed: false,
      phases: [{ name: "unit", passed: false, returncode: 1, stdout_tail: PYTEST_TAIL }],
    }));
    assert.equal(findOne(row, "conv-empty"), null,
      "'(no readable content for this record)' must be gone for a test round");
    const card = findOne(row, "step-report");
    assert.ok(card, "the round card renders inside the conversation bubble");
    assert.ok(card.classList.contains("kind-test"));
    assert.ok(row.textContent.includes("FAILED"));
    // The record's raw-JSON affordance is untouched.
    assert.ok(findOne(row, "raw-toggle"), "查看原始 must still be offered");
  });

  check("stream: a genuinely empty assistant record still shows the empty state", () => {
    const row = app.renderConversationRecord(app.normalizeRecord({
      step_id: "impl_1",
      step_type: "implement",
      message: { role: "assistant", content: "", raw_json: [] },
    }));
    const empty = findOne(row, "conv-empty");
    assert.ok(empty, "the untouched fallback must survive");
    assert.equal(empty.textContent, "(no readable content for this record)");
    assert.equal(findOne(row, "step-report"), null);
  });

  // =========================================================================
  // The terminal step_completed card reuses the SAME helper
  // =========================================================================
  check("terminal card: renderTestReport renders a failed phase through the shared helper", () => {
    // step.outputs.test_results keeps a FAILED phase's capture under the plain
    // `stdout` key — the same helper must read it.
    const frag = app.renderTestReport(
      { step_type: "test", status: "completed" },
      {
        test_results: {
          overall_passed: false,
          phases: [
            { name: "lint", passed: true, returncode: 0 },
            { name: "unit", passed: false, returncode: 1, stdout: PYTEST_TAIL },
          ],
          command: "pytest -q",
        },
      });
    assert.deepEqual(texts(frag, "step-report__icon"), ["✓", "✗"]);
    const sections = texts(frag, "step-report__section-title");
    assert.ok(sections.includes("Failure summary"),
      `the terminal card must reuse the failure summary: ${JSON.stringify(sections)}`);
    assert.ok(frag.textContent.includes("1 failed, 1 passed in 0.42s"));
    assert.ok(frag.textContent.includes("FAILED tests/test_alpha.py::test_two"));
    assert.ok(texts(frag, "step-report__muted").some((t) => t.includes("exit 1")));
    // Existing structure is preserved.
    assert.ok(frag.textContent.includes("1 / 2 phases"));
    assert.ok(sections.includes("Phases"));
    assert.ok(sections.includes("Command"));
  });

  check("terminal card: an all-passing report is unchanged — no failure sections", () => {
    const frag = app.renderTestReport(
      { step_type: "test", status: "completed" },
      { test_results: { overall_passed: true, phases: [{ name: "unit", passed: true }] } });
    assert.equal(findOne(frag, "step-report__label").textContent, "PASSED");
    assert.deepEqual(texts(frag, "step-report__icon"), ["✓"]);
    assert.ok(!texts(frag, "step-report__section-title").includes("Failure summary"));
  });
}
