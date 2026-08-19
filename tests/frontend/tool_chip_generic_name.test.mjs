/*
 * Generic (whitelist-free) tool-name parsing on the structured chip path.
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`) is installed. Exposes
 * `registerToolChipGenericNameTests({app, check, findOne, findAll})` so the
 * parent harness can drive the same check() reporter and the same `app`
 * module export.
 *
 * The bug under test: `applyFragmentToBubble` used to read a structured
 * fragment's tool name via the `TOOL_MARKER_NAMES` whitelist. Every tool
 * outside that handful — claude's `Agent` / `ReportFindings` / `ToolSearch` /
 * `Skill`, codex's synthesized `mcp__<server>__<tool>` / `unknown` — fell
 * through to the "Tool" fallback with an EMPTY header, so the terminal
 * fragment `[Agent ✓ …]` matched nothing, upgraded the chip with header "",
 * and left a chip reading only "Tool ✓".
 *
 * Coverage:
 *   (A1) Agent in-flight → success: chip is named Agent, header non-empty
 *   (A2) mcp__server__tool: the double-underscore name is taken whole
 *   (A3) an empty terminal header never blanks an existing header
 *   (A4) legacy `[Tool: Agent | Input: …]` → `[Agent ✓ …]` still upgrades
 *   (A5) `[Tool error: …]` keeps name=Tool / header="error: …"
 *   (A6) renderToolMarkers still ignores `[link](url)` in prose
 *   (A7) parseToolFragmentName unit behaviour
 */
import assert from "node:assert/strict";

export function registerToolChipGenericNameTests(ctx) {
  const { app, check, findOne, findAll } = ctx;

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

  const chipName = (chip) => {
    const n = findOne(chip, "tool-marker-name");
    return n ? n.textContent : "";
  };
  const chipHeader = (chip) => {
    const d = findOne(chip, "tool-marker-detail");
    return d ? d.textContent : "";
  };

  // Render one live pair (in-flight fragment then terminal fragment) sharing a
  // tool_use_id, and return the single resulting chip.
  const renderPair = (inFlight, terminal, extras = {}) => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      partialRecord(inFlight, 1, "s1", "implement", 0, { tool_use_id: "tu_g" }),
      partialRecord(terminal, 2, "s1", "implement", 0, {
        tool_use_id: "tu_g", is_error: false, ...extras,
      }),
    ], false);
    const chips = findAll(container, "tool-marker");
    assert.equal(chips.length, 1,
      `exactly one chip — upgraded in place, got ${chips.length}`);
    return chips[0];
  };

  // (A1) --------------------------------------------------------------------
  check("(A1) Agent in-flight→success keeps the Agent name and a non-empty header", () => {
    // Exact fragments the backend now emits for a subagent call:
    // format_tool_chip_in_flight_header / format_tool_chip_header.
    const chip = renderPair(
      "[Agent: description=self check, prompt=look at the diff]",
      "[Agent ✓ description=self check, prompt=look at the diff · No findings reported.]",
    );
    assert.equal(chip.classList.contains("success"), true);
    assert.equal(chipName(chip), "Agent",
      "chip is named after the real tool, not the 'Tool' whitelist fallback");
    const header = chipHeader(chip);
    assert.notEqual(header, "",
      "the completed chip must NOT lose its header (the reported bug)");
    assert.ok(header.includes("self check"), `header carries the input summary: '${header}'`);
    assert.ok(header.includes("No findings reported."),
      `header carries the result summary: '${header}'`);
  });

  // (A2) --------------------------------------------------------------------
  check("(A2) codex mcp__server__tool name is parsed whole and upgrades correctly", () => {
    const chip = renderPair(
      "[mcp__context7__get-library-docs: library=fastapi]",
      "[mcp__context7__get-library-docs ✓ library=fastapi · docs…]",
    );
    assert.equal(chipName(chip), "mcp__context7__get-library-docs",
      "double underscores and hyphens stay inside the name token");
    assert.ok(chipHeader(chip).includes("library=fastapi"));
    assert.equal(chip.classList.contains("success"), true);
  });

  check("(A2b) codex 'unknown' tool name renders as its own chip name", () => {
    const chip = renderPair(
      "[unknown: raw=no tool name in the event]",
      "[unknown ✓ raw=no tool name in the event · done]",
    );
    assert.equal(chipName(chip), "unknown");
    assert.notEqual(chipHeader(chip), "");
  });

  // (A3) --------------------------------------------------------------------
  check("(A3) an empty terminal header never blanks the in-flight header", () => {
    // A fragment that carries no parseable header at all (defensive: a
    // truncated / malformed terminal record). The chip must keep showing what
    // the in-flight fragment already told the user.
    const container = document.createElement("div");
    app.renderConversation(container, [
      partialRecord("[Agent: prompt=do the thing]", 1, "s1", "implement", 0, {
        tool_use_id: "tu_blank",
      }),
      partialRecord("[Agent ✓]", 2, "s1", "implement", 0, {
        tool_use_id: "tu_blank", is_error: false,
      }),
    ], false);
    const chips = findAll(container, "tool-marker");
    assert.equal(chips.length, 1);
    assert.equal(chipName(chips[0]), "Agent");
    assert.equal(chipHeader(chips[0]), "prompt=do the thing",
      "the pre-existing header survives an empty terminal header");
  });

  check("(A3b) upgradeChipToSuccess called directly with '' preserves the header", () => {
    const chip = app.createInFlightChip("Agent", "prompt=x");
    app.upgradeChipToSuccess(chip, "", null);
    assert.equal(app.chipHeaderText(chip), "prompt=x");
    assert.equal(chip.classList.contains("success"), true);
  });

  check("(A3c) upgradeChipToFailure called directly with '' preserves the header", () => {
    const chip = app.createInFlightChip("Agent", "prompt=x");
    app.upgradeChipToFailure(chip, "", null);
    assert.equal(app.chipHeaderText(chip), "prompt=x");
    assert.equal(chip.classList.contains("failure"), true);
  });

  // (A4) --------------------------------------------------------------------
  check("(A4) legacy '[Tool: Agent | Input: …]' + '[Agent ✓ …]' still upgrades to Agent", () => {
    // Old jsonl on disk carries the pre-fix in-flight framing. Its name token
    // is the literal "Tool", so the chip starts out named Tool — but the
    // terminal fragment names the real tool and must rename + rebuild the head
    // rather than leave a bare "Tool ✓".
    const chip = renderPair(
      "[Tool: Agent | Input: description=self check, prompt=look]",
      "[Agent ✓ description=self check, prompt=look · No findings reported.]",
    );
    assert.equal(chipName(chip), "Agent",
      "the terminal fragment's real name replaces the legacy 'Tool' fallback");
    assert.ok(chipHeader(chip).includes("No findings reported."));
    assert.equal(chip.classList.contains("success"), true);
  });

  check("(A4b) legacy in-flight alone still renders as a Tool-named chip", () => {
    // Backward compatibility invariant: an old in-flight record with no
    // terminal partner renders exactly as it always did (name=Tool), never a
    // blank chip.
    const container = document.createElement("div");
    app.renderConversation(container, [
      partialRecord("[Tool: Agent | Input: prompt=look]", 1, "s1", "implement", 0, {
        tool_use_id: "tu_legacy",
      }),
    ], false);
    const chips = findAll(container, "tool-marker");
    assert.equal(chips.length, 1);
    assert.equal(chipName(chips[0]), "Tool");
    assert.ok(chipHeader(chips[0]).includes("Agent"),
      `legacy header keeps its text: '${chipHeader(chips[0])}'`);
    assert.equal(chips[0].classList.contains("in-flight"), true);
  });

  // (A5) --------------------------------------------------------------------
  check("(A5) '[Tool error: …]' keeps name=Tool and header='error: …'", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      partialRecord("[Tool error: stream closed]", 1, "s1", "implement", 0, {
        tool_use_id: "tu_orphan", is_error: true,
      }),
    ], false);
    const chips = findAll(container, "tool-marker");
    assert.equal(chips.length, 1);
    assert.equal(chipName(chips[0]), "Tool");
    assert.equal(chipHeader(chips[0]), "error: stream closed");
    assert.equal(chips[0].classList.contains("failure"), true);
  });

  // (A6) --------------------------------------------------------------------
  check("(A6) renderToolMarkers still ignores Markdown links in prose", () => {
    // The legacy prose-slicing path deliberately KEEPS the TOOL_MARKER_NAMES
    // whitelist — going generic there would turn every `[link](url)` and every
    // bracketed aside into a tool chip.
    const wrap = document.createElement("div");
    for (const node of app.renderToolMarkers(
      "See [the docs](https://example.com) and [RFC 42] for details."
    )) wrap.appendChild(node);
    assert.equal(findAll(wrap, "tool-marker").length, 0,
      "prose brackets must never render as tool chips");
    assert.ok(wrap.textContent.includes("the docs"),
      "the prose itself still renders");
  });

  check("(A6b) renderToolMarkers still slices a whitelisted inline marker", () => {
    const wrap = document.createElement("div");
    for (const node of app.renderToolMarkers(
      "before [Read: src/foo.py:0-200] after"
    )) wrap.appendChild(node);
    const chips = findAll(wrap, "tool-marker");
    assert.equal(chips.length, 1);
    assert.equal(chipName(chips[0]), "Read");
  });

  check("(A6c) renderToolMarkers leaves a non-whitelisted bracket as prose", () => {
    // Unchanged legacy behaviour: `[Agent ✓ …]` inline in free text is NOT
    // sliced into a chip. Only the structured (tool_use_id) path renders any
    // tool name generically.
    const wrap = document.createElement("div");
    for (const node of app.renderToolMarkers("prose [Agent ✓ done] more prose")) {
      wrap.appendChild(node);
    }
    assert.equal(findAll(wrap, "tool-marker").length, 0);
  });

  // (A7) --------------------------------------------------------------------
  check("(A7) parseToolFragmentName reads the leading token, or null", () => {
    assert.equal(app.parseToolFragmentName("[Agent: prompt=x]"), "Agent");
    assert.equal(app.parseToolFragmentName("[Agent ✓ done]"), "Agent");
    assert.equal(app.parseToolFragmentName("[Agent ✗ boom]"), "Agent");
    assert.equal(app.parseToolFragmentName("[Read: a.py:0-200]"), "Read");
    assert.equal(
      app.parseToolFragmentName("[mcp__ctx7__get-library-docs: q=1]"),
      "mcp__ctx7__get-library-docs");
    assert.equal(app.parseToolFragmentName("[Tool error: x]"), "Tool");
    assert.equal(app.parseToolFragmentName("[unknown ✓ done]"), "unknown");
    // No bracket / no name token → null, so the caller falls back to "Tool".
    assert.equal(app.parseToolFragmentName("plain text"), null);
    assert.equal(app.parseToolFragmentName("[✓ done]"), null);
    assert.equal(app.parseToolFragmentName(""), null);
    assert.equal(app.parseToolFragmentName(null), null);
  });

  check("(A7b) parseToolBracket on a generic name yields a clean header", () => {
    const p = app.parseToolBracket("Agent", "[Agent ✓ prompt=x · done]");
    assert.equal(p.name, "Agent");
    assert.equal(p.status, "success");
    assert.equal(p.header, "prompt=x · done");
  });
}
