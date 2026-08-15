/*
 * Tool-chip state machine tests (Group G3).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`) is installed. Exposes
 * `registerToolChipStateTests({app, check, makeContainer, findOne, findAll})`
 * so the parent harness can drive the same check() reporter and the same
 * `app` module export.
 *
 * Coverage:
 *   (a) tool_use stream_progress → in-flight chip
 *   (b) tool_use + tool_result(success) → upgraded chip + detail panel
 *   (c) tool_use + tool_result(failure) → failure chip, detail default-expanded
 *   (d) legacy partial record without tool_use_id → still renders an in-flight
 *       chip (falls back to the bracket-marker parse), never the empty zombie
 *       second chip the pre-G3 code emitted
 *   (e) live partial sequence vs final raw_json sequence → equivalent chip
 *       structure (same chip count, names, statuses, headers)
 */
import assert from "node:assert/strict";

export function registerToolChipStateTests(ctx) {
  const { app, check, findOne, findAll } = ctx;

  // ---- helpers ----------------------------------------------------------
  // partial / final / asst record shapes mirror the parent test harness.
  const partialRecord = (
    content, ts, stepId, stepType, attempt, extras = {},
  ) => ({
    step_id: stepId,
    step_type: stepType,
    message: {
      type: "stream_progress",
      role: "assistant",
      content,
      timestamp: ts,
      attempt,
      partial: true,
      ...extras,
    },
  });

  // Final non-partial assistant record carrying tool_use + tool_result blocks
  // in its raw_json — the shape Claude CLI emits for a fully-completed turn.
  const finalRecordWithToolBlocks = (
    stepId, stepType, blocks, ts = 100, attempt = 0,
  ) => ({
    step_id: stepId,
    step_type: stepType,
    message: {
      role: "assistant",
      content: "",
      timestamp: ts,
      attempt,
      raw_json: [
        { type: "assistant", message: { content: blocks } },
      ],
    },
  });

  // Describe a chip's salient state for structural comparison: name, status
  // class, header text. Detail panel content is intentionally *not* compared —
  // the live path carries the rich `tool_detail` payload, the final raw_json
  // path falls back to a text-kind detail synthesized from the block content,
  // so they would differ. Spec acceptance is "equivalent chip shape", not
  // byte-identical detail panels.
  const describeChip = (chip) => {
    const name = findOne(chip, "tool-marker-name");
    const detail = findOne(chip, "tool-marker-detail");
    let status = "in-flight";
    if (chip.classList.contains("success")) status = "success";
    else if (chip.classList.contains("failure")) status = "failure";
    return {
      name: name ? name.textContent : "",
      header: detail ? detail.textContent : "",
      status: status,
    };
  };

  const describeChipsIn = (container) =>
    findAll(container, "tool-marker").map(describeChip);

  // (a) -------------------------------------------------------------------
  check("(a) tool_use partial creates an in-flight chip via state machine", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      partialRecord("[Read: src/foo.py:0-200]", 1, "s1", "implement", 0, {
        tool_use_id: "tu_1",
      }),
    ], false);
    const chips = findAll(container, "tool-marker");
    assert.equal(chips.length, 1, "exactly one chip for one in-flight tool_use");
    assert.equal(chips[0].classList.contains("in-flight"), true);
    assert.equal(chips[0].classList.contains("success"), false);
    assert.equal(chips[0].classList.contains("failure"), false);
    // Detail panel is NOT attached for in-flight chips (no `tool_detail`).
    assert.equal(findOne(chips[0], "tool-marker-details"), null);
  });

  // (b) -------------------------------------------------------------------
  check("(b) tool_use + tool_result(success) upgrades same chip; one chip, detail folded", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      partialRecord("[Read: src/foo.py:0-200]", 1, "s1", "implement", 0, {
        tool_use_id: "tu_1",
      }),
      partialRecord("[Read ✓ src/foo.py:0-200 · 87 lines]", 2, "s1", "implement", 0, {
        tool_use_id: "tu_1",
        is_error: false,
        tool_detail: {
          kind: "read_text",
          file_path: "src/foo.py",
          text: "line1\nline2\nline3",
          start_line: 1,
          truncated: false,
        },
      }),
    ], false);
    const chips = findAll(container, "tool-marker");
    assert.equal(chips.length, 1,
      "exactly one chip — upgraded in place by tool_use_id, no zombie second chip");
    assert.equal(chips[0].classList.contains("in-flight"), false);
    assert.equal(chips[0].classList.contains("success"), true);
    // Success chip carries a detail panel, FOLDED by default.
    const panel = findOne(chips[0], "tool-marker-details");
    assert.ok(panel, "success chip has a detail panel");
    assert.equal(panel.classList.contains("folded"), true,
      "success detail panel folded by default");
    assert.equal(panel.classList.contains("expanded"), false);
    // The expanded body must contain the rendered read_text payload (line gutter + text).
    const body = findOne(panel, "tool-marker-details-body");
    assert.ok(body.textContent.includes("line1"), "detail body carries read_text content");
  });

  // (c) -------------------------------------------------------------------
  check("(c) tool_use + tool_result(failure) marks chip failure; detail default-expanded", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      partialRecord("[Edit: src/bar.py]", 1, "s1", "implement", 0, {
        tool_use_id: "tu_e",
      }),
      partialRecord("[Edit ✗ ENOENT src/bar.py]", 2, "s1", "implement", 0, {
        tool_use_id: "tu_e",
        is_error: true,
        tool_detail: { kind: "text", text: "ENOENT: no such file or directory" },
      }),
    ], false);
    const chips = findAll(container, "tool-marker");
    assert.equal(chips.length, 1);
    assert.equal(chips[0].classList.contains("failure"), true);
    assert.equal(chips[0].classList.contains("in-flight"), false);
    const panel = findOne(chips[0], "tool-marker-details");
    assert.ok(panel, "failure chip has a detail panel");
    assert.equal(panel.classList.contains("expanded"), true,
      "failure detail panel default-expanded so the error is visible");
    const body = findOne(panel, "tool-marker-details-body");
    assert.ok(body.textContent.includes("ENOENT"),
      "failure detail body carries the error preview");
  });

  // (d) -------------------------------------------------------------------
  check("(d) legacy partial without tool_use_id still renders an in-flight chip (no zombie)", () => {
    // Pre-G3 jsonl stream_progress records had no tool_use_id at the envelope.
    // The chip state machine cannot pair them, so they degrade to the legacy
    // bracket-marker parser path — which must still render the `[Name: …]`
    // marker as a `.tool-marker` chip rather than the empty zombie that the
    // pre-G3 colon-split produced on the success/failure form.
    const container = document.createElement("div");
    app.renderConversation(container, [
      partialRecord("[Read: src/foo.py:0-200]", 1, "s1", "implement", 0),
    ], false);
    const chips = findAll(container, "tool-marker");
    assert.equal(chips.length, 1);
    const name = findOne(chips[0], "tool-marker-name");
    assert.equal(name && name.textContent, "Read");
    // Detail span carries the header text (path + range), NOT an empty string.
    const detail = findOne(chips[0], "tool-marker-detail");
    assert.ok(detail, "legacy bracket chip has a header span");
    assert.ok(detail.textContent.includes("src/foo.py"),
      `legacy bracket chip header carries the path, got '${detail && detail.textContent}'`);
  });

  // (d2) ------------------------------------------------------------------
  check("(d2) legacy [Read ✓ …] bracket-only marker now renders a success chip with non-empty header", () => {
    // The bug this group fixes: the pre-G3 renderToolBlock used
    // inner.indexOf(":") to slice the header, so `[Read ✓ src/foo.py · 87 lines]`
    // (no colon) yielded an empty header → an empty chip rendered next to the
    // in-flight one. The new parseToolBracket-based renderer must produce a
    // success-classed chip whose header carries the actual text.
    const container = document.createElement("div");
    app.renderConversation(container, [
      partialRecord("[Read ✓ src/foo.py · 87 lines]", 1, "s1", "implement", 0),
    ], false);
    const chips = findAll(container, "tool-marker");
    assert.equal(chips.length, 1, "exactly one chip — never a zombie second chip");
    assert.equal(chips[0].classList.contains("success"), true,
      "✓ glyph routes the chip to the success state");
    const detail = findOne(chips[0], "tool-marker-detail");
    assert.ok(detail && detail.textContent.includes("src/foo.py"),
      `legacy success header carries the path, got '${detail && detail.textContent}'`);
  });

  // (e) -------------------------------------------------------------------
  check("(e) live partial sequence vs final raw_json sequence produce equivalent chips", () => {
    // Live: two partials carrying structured tool_use_id / is_error / tool_detail.
    // The header on the live success bracket is the per-tool combined body
    // produced by `format_tool_chip_header` on the Python side, so the live
    // bracket text and the final-view JS formatters MUST match.
    const liveContainer = document.createElement("div");
    app.renderConversation(liveContainer, [
      partialRecord("[Read: src/foo.py:0-200]", 1, "s1", "implement", 0, {
        tool_use_id: "tu_x",
      }),
      partialRecord("[Read ✓ src/foo.py:0-200 · 1 lines]", 2, "s1", "implement", 0, {
        tool_use_id: "tu_x",
        is_error: false,
        tool_detail: {
          kind: "read_text", file_path: "src/foo.py",
          text: "ok", start_line: 1, truncated: false,
        },
      }),
    ], false);
    const liveChips = describeChipsIn(liveContainer);

    // Final: ONE non-partial assistant record carrying both tool_use and
    // tool_result blocks in its raw_json — the shape Claude CLI emits for a
    // completed turn.
    const finalContainer = document.createElement("div");
    app.renderConversation(finalContainer, [
      finalRecordWithToolBlocks("s1", "implement", [
        { type: "tool_use", id: "tu_x", name: "Read",
          input: { file_path: "src/foo.py", offset: 0, limit: 200 } },
        { type: "tool_result", tool_use_id: "tu_x",
          content: [{ type: "text", text: "ok" }], is_error: false },
      ]),
    ], false);
    const finalChips = describeChipsIn(finalContainer);

    // Full equivalence: same chip count + name + status + header. The header
    // body MUST match because the JS-side per-tool formatters mirror the
    // Python `_success_combined_*` table.
    assert.equal(liveChips.length, 1, "live: one merged chip");
    assert.equal(finalChips.length, 1, "final: one merged chip (no zombie second chip)");
    assert.equal(liveChips[0].name, finalChips[0].name,
      "tool name matches across live and final");
    assert.equal(liveChips[0].status, finalChips[0].status,
      "status (success) matches across live and final");
    assert.equal(liveChips[0].header, finalChips[0].header,
      `header body matches across live and final, got live='${liveChips[0].header}' final='${finalChips[0].header}'`);
    assert.ok(finalChips[0].header.includes("src/foo.py"),
      `final chip header carries the file path body (not raw JSON), got '${finalChips[0].header}'`);
    assert.ok(!finalChips[0].header.startsWith("{"),
      `final chip header is NOT a JSON.stringify dump, got '${finalChips[0].header}'`);
  });

  // (e3) ------------------------------------------------------------------
  check("(e3) final raw_json Edit chip renders per-tool header + unified-diff detail", () => {
    // Regression: pre-fix the final chip header was `JSON.stringify(input)`
    // and the detail panel was a `{kind:'text'}` carrying the raw result
    // string. Now it must be the per-tool body `path (N→M lines)` and a
    // `kind:'edit_diff'` detail rendered as a +/- coloured diff.
    const container = document.createElement("div");
    app.renderConversation(container, [
      finalRecordWithToolBlocks("s1", "implement", [
        { type: "tool_use", id: "tu_e", name: "Edit",
          input: {
            file_path: "src/bar.py",
            old_string: "alpha\nbeta\ngamma",
            new_string: "alpha\nBETA\ngamma",
          } },
        { type: "tool_result", tool_use_id: "tu_e",
          content: [{ type: "text", text: "edit applied" }], is_error: false },
      ]),
    ], false);
    const chips = findAll(container, "tool-marker");
    assert.equal(chips.length, 1, "one merged Edit chip");
    assert.equal(chips[0].classList.contains("success"), true);
    const headerSpan = findOne(chips[0], "tool-marker-detail");
    assert.ok(headerSpan, "Edit chip has a header span");
    assert.equal(headerSpan.textContent, "src/bar.py (3 lines → 3 lines)",
      `Edit header is the per-tool body, got '${headerSpan.textContent}'`);
    assert.ok(!headerSpan.textContent.includes("old_string"),
      "Edit header does NOT contain raw JSON keys");
    // Detail panel must carry the unified-diff DOM, not a plain text dump.
    const panel = findOne(chips[0], "tool-marker-details");
    assert.ok(panel, "Edit chip has a detail panel");
    const diffWrap = findOne(panel, "tool-marker-diff");
    assert.ok(diffWrap, "detail panel renders the unified-diff layout (tool-marker-diff)");
    const addLines = findAll(panel, "diff-add");
    const delLines = findAll(panel, "diff-del");
    assert.ok(addLines.length >= 1, "diff has at least one + line");
    assert.ok(delLines.length >= 1, "diff has at least one - line");
    const body = findOne(panel, "tool-marker-details-body");
    assert.ok(!body.textContent.includes("edit applied"),
      "Edit detail panel does NOT fall back to the raw result string");
  });

  // (f) -------------------------------------------------------------------
  // Toggle reposition: the details toggle button MUST sit as a direct child
  // of the chip's head row (right-aligned via `.tool-marker-toggle`'s
  // `margin-left:auto` rule), and appear BEFORE the `.tool-marker-details`
  // panel. The old layout buried the toggle inside the panel as a
  // `.tool-marker-details-toggle`, producing a lonely second-row "details"
  // button under a dashed separator.
  check("(f) details toggle is a chip-head sibling positioned before .tool-marker-details", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      partialRecord("[Read: src/foo.py:0-200]", 1, "s1", "implement", 0, {
        tool_use_id: "tu_f",
      }),
      partialRecord("[Read ✓ src/foo.py:0-200 · 3 lines]", 2, "s1", "implement", 0, {
        tool_use_id: "tu_f",
        is_error: false,
        tool_detail: {
          kind: "read_text",
          file_path: "src/foo.py",
          text: "a\nb\nc",
          start_line: 1,
          truncated: false,
        },
      }),
    ], false);
    const chips = findAll(container, "tool-marker");
    assert.equal(chips.length, 1);
    const chip = chips[0];
    // Toggle is a DIRECT child of the chip (not nested inside details panel).
    const toggle = chip.children.find(
      (c) => c.classList && c.classList.contains("tool-marker-toggle"));
    assert.ok(toggle, "toggle is a direct chip child");
    // Legacy class name must not be used at the chip layer.
    const legacyToggle = findOne(chip, "tool-marker-details-toggle");
    assert.equal(legacyToggle, null,
      "old .tool-marker-details-toggle class must be retired");
    // The details panel is a sibling of the toggle, also a direct chip child.
    const panel = chip.children.find(
      (c) => c.classList && c.classList.contains("tool-marker-details"));
    assert.ok(panel, "details panel is a direct chip child");
    // DOM order: toggle appears BEFORE the details panel in the chip's
    // children list so the chip-head row terminates with the right-aligned
    // toggle and the panel wraps to its own row underneath.
    const toggleIdx = chip.children.indexOf(toggle);
    const panelIdx = chip.children.indexOf(panel);
    assert.ok(toggleIdx >= 0 && panelIdx >= 0);
    assert.ok(toggleIdx < panelIdx,
      `toggle must precede details panel in chip children, got toggle=${toggleIdx} panel=${panelIdx}`);
    // Head DOM order: name → detail header → glyph → toggle → panel.
    const name = chip.children.find(
      (c) => c.classList && c.classList.contains("tool-marker-name"));
    const detail = chip.children.find(
      (c) => c.classList && c.classList.contains("tool-marker-detail"));
    const glyph = chip.children.find(
      (c) => c.classList && c.classList.contains("tool-marker-glyph"));
    const nameIdx = chip.children.indexOf(name);
    const detailIdx = chip.children.indexOf(detail);
    const glyphIdx = chip.children.indexOf(glyph);
    assert.ok(nameIdx >= 0 && detailIdx >= 0 && glyphIdx >= 0);
    assert.ok(nameIdx < detailIdx && detailIdx < glyphIdx && glyphIdx < toggleIdx,
      "head row order is name → detail → glyph → toggle");
    // The panel body still occupies the wrapping second row; toggle no
    // longer lives inside the panel.
    const body = findOne(panel, "tool-marker-details-body");
    assert.ok(body, "panel still hosts the details body");
    assert.equal(findOne(panel, "tool-marker-toggle"), null,
      "toggle is NOT nested inside the details panel anymore");
  });

  // (f2) ------------------------------------------------------------------
  // Successive upgrades (in-flight → success → failure) must NOT leave behind
  // old toggles or details panels. attachChipDetail wipes both before
  // re-appending fresh nodes.
  check("(f2) re-upgrade replaces toggle and details panel without duplicates", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      partialRecord("[Edit: src/bar.py]", 1, "s1", "implement", 0, {
        tool_use_id: "tu_re",
      }),
      partialRecord("[Edit ✓ src/bar.py (3 lines → 3 lines)]", 2, "s1", "implement", 0, {
        tool_use_id: "tu_re",
        is_error: false,
        tool_detail: {
          kind: "text", text: "first result",
        },
      }),
      partialRecord("[Edit ✗ ENOENT src/bar.py]", 3, "s1", "implement", 0, {
        tool_use_id: "tu_re",
        is_error: true,
        tool_detail: { kind: "text", text: "ENOENT: no such file" },
      }),
    ], false);
    const chips = findAll(container, "tool-marker");
    assert.equal(chips.length, 1);
    const chip = chips[0];
    const toggles = chip.children.filter(
      (c) => c.classList && c.classList.contains("tool-marker-toggle"));
    const panels = chip.children.filter(
      (c) => c.classList && c.classList.contains("tool-marker-details"));
    assert.equal(toggles.length, 1,
      "exactly one toggle remains after successive upgrades");
    assert.equal(panels.length, 1,
      "exactly one details panel remains after successive upgrades");
    // Final state is failure → panel default-expanded.
    assert.equal(panels[0].classList.contains("expanded"), true);
  });

  // (f3) ------------------------------------------------------------------
  // In-flight chips have no detail data, so no toggle should appear.
  check("(f3) in-flight chip without detail renders no toggle", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      partialRecord("[Read: src/foo.py:0-200]", 1, "s1", "implement", 0, {
        tool_use_id: "tu_inflight",
      }),
    ], false);
    const chips = findAll(container, "tool-marker");
    assert.equal(chips.length, 1);
    const chip = chips[0];
    assert.equal(findOne(chip, "tool-marker-toggle"), null,
      "no toggle when there is no detail to disclose");
    assert.equal(findOne(chip, "tool-marker-details"), null,
      "no details panel when there is no detail to disclose");
  });

  // (e2) ------------------------------------------------------------------
  check("(e2) final raw_json with mismatched / orphan tool_result still produces one chip per id", () => {
    // Defensive shape: a tool_result with no preceding tool_use becomes its own
    // chip in the terminal state (success / failure determined by is_error).
    const container = document.createElement("div");
    app.renderConversation(container, [
      finalRecordWithToolBlocks("s1", "implement", [
        { type: "tool_use", id: "tu_a", name: "Bash",
          input: { command: "ls -la" } },
        { type: "tool_result", tool_use_id: "tu_a",
          content: [{ type: "text", text: "file1\nfile2" }], is_error: false },
        // Orphan result with no matching use:
        { type: "tool_result", tool_use_id: "tu_orphan",
          content: [{ type: "text", text: "stray" }], is_error: false },
      ]),
    ], false);
    const chips = findAll(container, "tool-marker");
    assert.equal(chips.length, 2,
      "one chip per id pair, orphan result renders its own chip");
    // No `.in-flight` chips remain — every chip resolved.
    for (const c of chips) {
      assert.equal(c.classList.contains("in-flight"), false,
        "every chip resolved to success/failure");
    }
  });

  // (g1) ------------------------------------------------------------------
  check("(g1) Write whose input carries no `content` key renders path only, never a delete-everything diff", () => {
    // Shape emitted by the codex runner for a `file_change` item: codex reports
    // *that* a file changed and never its text, so the input has `file_path`
    // alone. Rendering "(empty)" or a diff against the (already written) file
    // would claim the file was emptied.
    const container = document.createElement("div");
    app.renderConversation(container, [
      finalRecordWithToolBlocks("s1", "implement", [
        { type: "tool_use", id: "tu_w", name: "Write",
          input: { file_path: "src/b.py" } },
        { type: "tool_result", tool_use_id: "tu_w",
          content: [{ type: "text", text: "File add: src/b.py" }], is_error: false },
      ]),
    ], false);
    const chips = findAll(container, "tool-marker");
    assert.equal(chips.length, 1, "one Write chip");
    const headerSpan = findOne(chips[0], "tool-marker-detail");
    assert.equal(headerSpan.textContent, "src/b.py",
      `header is the bare path, got '${headerSpan.textContent}'`);
    assert.ok(!headerSpan.textContent.includes("lines"),
      "header carries no line count nobody measured");
    assert.ok(!headerSpan.textContent.includes("empty"),
      "header does not claim the file is empty");
    const panel = findOne(chips[0], "tool-marker-details");
    assert.ok(panel, "chip still has a detail panel");
    assert.equal(findAll(panel, "diff-del").length, 0,
      "detail panel renders no deletion lines");
    assert.equal(findAll(panel, "diff-add").length, 0,
      "detail panel renders no addition lines");
    assert.ok(findOne(panel, "tool-detail-empty"),
      "detail panel says no content was reported");
    assert.ok(panel.textContent.includes("src/b.py"),
      "detail panel still shows the path");
  });

  // (g2) ------------------------------------------------------------------
  check("(g2) Write with a present-but-empty `content` keeps its (empty) / 0-line rendering", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      finalRecordWithToolBlocks("s1", "implement", [
        { type: "tool_use", id: "tu_w2", name: "Write",
          input: { file_path: "src/e.py", content: "" } },
        { type: "tool_result", tool_use_id: "tu_w2",
          content: [{ type: "text", text: "ok" }], is_error: false },
      ]),
    ], false);
    const headerSpan = findOne(findAll(container, "tool-marker")[0], "tool-marker-detail");
    assert.ok(headerSpan.textContent.includes("lines"),
      `an empty write really is 0 lines, got '${headerSpan.textContent}'`);
  });

  // (g3) ------------------------------------------------------------------
  check("(g3) Edit whose input carries neither old_string nor new_string renders path only", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      finalRecordWithToolBlocks("s1", "implement", [
        { type: "tool_use", id: "tu_e2", name: "Edit",
          input: { file_path: "src/c.py" } },
        { type: "tool_result", tool_use_id: "tu_e2",
          content: [{ type: "text", text: "File update: src/c.py" }], is_error: false },
      ]),
    ], false);
    const chip = findAll(container, "tool-marker")[0];
    const headerSpan = findOne(chip, "tool-marker-detail");
    assert.equal(headerSpan.textContent, "src/c.py",
      `header is the bare path, got '${headerSpan.textContent}'`);
    const panel = findOne(chip, "tool-marker-details");
    assert.equal(findOne(panel, "tool-marker-diff"), null,
      "no unified-diff layout when there is no text to diff");
    assert.ok(findOne(panel, "tool-detail-empty"),
      "detail panel says no content was reported");
  });

  // (g4) ------------------------------------------------------------------
  check("(g4) unregistered file tool (codex Delete) keeps the filename in its header", () => {
    // codex `file_change` with kind=delete becomes tool_use name="Delete",
    // input={file_path}. "Delete" has no per-tool formatter, so both the Python
    // chip header and this JS mirror go through the generic key=value body — a
    // blind 30-char truncation there would drop the filename, leaving the user
    // unable to tell which file codex deleted.
    const longPath = "/data/cre/workspace/se3.0/tianluo/src/tianluo/engine/tool_formatters.py";
    const container = document.createElement("div");
    app.renderConversation(container, [
      finalRecordWithToolBlocks("s1", "implement", [
        { type: "tool_use", id: "tu_del", name: "Delete",
          input: { file_path: longPath } },
        { type: "tool_result", tool_use_id: "tu_del",
          content: [{ type: "text", text: "File delete: tool_formatters.py" }],
          is_error: false },
      ]),
    ], false);
    const chip = findAll(container, "tool-marker")[0];
    const headerSpan = findOne(chip, "tool-marker-detail");
    assert.ok(headerSpan.textContent.startsWith("file_path="),
      `generic key=value body, got '${headerSpan.textContent}'`);
    assert.ok(headerSpan.textContent.includes("tool_formatters.py"),
      `filename survives, got '${headerSpan.textContent}'`);
    assert.ok(!headerSpan.textContent.includes("se3.0/t..."),
      `no mid-path cut, got '${headerSpan.textContent}'`);
  });

  // (g5) ------------------------------------------------------------------
  check("(g5) an over-long file_path abbreviates in the middle, never the tail", () => {
    // Mirrors truncate_path's step 2: first segment + '/.../' + filename.
    const deep = "/root/" + Array.from({ length: 30 }, (_, i) => `segment_${i}`).join("/") + "/victim.py";
    const container = document.createElement("div");
    app.renderConversation(container, [
      finalRecordWithToolBlocks("s1", "implement", [
        { type: "tool_use", id: "tu_del2", name: "Delete",
          input: { file_path: deep } },
        { type: "tool_result", tool_use_id: "tu_del2",
          content: [{ type: "text", text: "ok" }], is_error: false },
      ]),
    ], false);
    const headerSpan = findOne(findAll(container, "tool-marker")[0], "tool-marker-detail");
    assert.ok(headerSpan.textContent.includes("victim.py"),
      `filename is never truncated, got '${headerSpan.textContent}'`);
    assert.ok(headerSpan.textContent.includes("/.../"),
      `middle is abbreviated, got '${headerSpan.textContent}'`);
  });

  // (g6) ------------------------------------------------------------------
  check("(g6) non-path string values in a generic body still truncate at 30 chars", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      finalRecordWithToolBlocks("s1", "implement", [
        { type: "tool_use", id: "tu_x", name: "SomeTool",
          input: { note: "x".repeat(80) } },
        { type: "tool_result", tool_use_id: "tu_x",
          content: [{ type: "text", text: "ok" }], is_error: false },
      ]),
    ], false);
    const headerSpan = findOne(findAll(container, "tool-marker")[0], "tool-marker-detail");
    assert.ok(headerSpan.textContent.includes("..."),
      `long non-path values keep the old preview truncation, got '${headerSpan.textContent}'`);
    assert.ok(!headerSpan.textContent.includes("x".repeat(40)),
      `value was shortened, got '${headerSpan.textContent}'`);
  });
}
