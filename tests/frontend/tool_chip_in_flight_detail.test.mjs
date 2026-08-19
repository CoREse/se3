/*
 * Expandable detail panel on an IN-FLIGHT tool chip.
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`) is installed. Exposes
 * `registerToolChipInFlightDetailTests({app, check, findOne, findAll})` so the
 * parent harness can drive the same check() reporter and the same `app`
 * module export.
 *
 * The gap under test: a running tool call rendered as a chip whose header is a
 * 60-char summary and which could not be opened at all — the full Bash command
 * or Agent prompt was simply unavailable until the call settled. The backend
 * now attaches a `kind:"tool_input"` payload to the in-flight
 * `stream_progress` record, and the settled payload of an unregistered tool
 * carries the call's `input` alongside its result text.
 *
 * Coverage:
 *   (B1) an in-flight chip with tool_detail gets a folded, openable panel
 *   (B2) upgrading that chip leaves exactly one toggle + one panel
 *   (B3) the tool_input renderer: Bash `$ command`, generic key/value list
 *   (B4) the text renderer shows input before result
 *   (B5) extractAssistantChipEvents gives an unsettled tool_use a detail
 *   (B6) an unregistered tool's terminal detail carries `input`
 *   (B7) in-flight is still keyed off `is_error`, never off `tool_detail`
 */
import assert from "node:assert/strict";

export function registerToolChipInFlightDetailTests(ctx) {
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

  const agentInput = {
    description: "self check",
    prompt: "look at the diff\nand report findings",
  };
  const agentInFlightDetail = {
    kind: "tool_input",
    tool_name: "Agent",
    input: agentInput,
    truncated: false,
  };

  const chipHeader = (chip) => {
    const d = findOne(chip, "tool-marker-detail");
    return d ? d.textContent : "";
  };

  // Render live fragments and return the single chip they produce.
  const renderLive = (records) => {
    const container = document.createElement("div");
    app.renderConversation(container, records, false);
    const chips = findAll(container, "tool-marker");
    assert.equal(chips.length, 1, `expected one chip, got ${chips.length}`);
    return chips[0];
  };

  // (B1) --------------------------------------------------------------------
  check("(B1) an in-flight chip carrying tool_detail gets a folded detail panel", () => {
    const chip = renderLive([
      partialRecord("[Agent: description=self check, prompt=look at the diff]",
        1, "s1", "implement", 0,
        { tool_use_id: "tu_b1", tool_detail: agentInFlightDetail }),
    ]);
    assert.equal(chip.classList.contains("in-flight"), true,
      "still in-flight — no is_error was seen");
    const panel = findOne(chip, "tool-marker-details");
    assert.ok(panel, "a running call must be expandable");
    assert.equal(panel.classList.contains("folded"), true,
      "default folded so the streaming conversation is not pushed around");
    assert.equal(panel.classList.contains("expanded"), false);
    const toggle = findOne(chip, "tool-marker-toggle");
    assert.ok(toggle, "the panel needs its toggle button");
  });

  check("(B1b) the in-flight panel actually holds the full input", () => {
    const chip = renderLive([
      partialRecord("[Agent: description=self check, prompt=look at the diff]",
        1, "s1", "implement", 0,
        { tool_use_id: "tu_b1b", tool_detail: agentInFlightDetail }),
    ]);
    const body = findOne(chip, "tool-marker-details-body");
    assert.ok(body);
    const text = body.textContent || "";
    assert.ok(text.includes("look at the diff"),
      `the full prompt is readable in the panel: '${text}'`);
    assert.ok(text.includes("and report findings"),
      "the multi-line tail the 60-char header dropped is present too");
  });

  check("(B1c) an in-flight chip WITHOUT tool_detail stays panel-free (legacy jsonl)", () => {
    const chip = renderLive([
      partialRecord("[Agent: prompt=look]", 1, "s1", "implement", 0,
        { tool_use_id: "tu_b1c" }),
    ]);
    assert.equal(findOne(chip, "tool-marker-details"), null);
    assert.equal(findOne(chip, "tool-marker-toggle"), null);
  });

  check("(B1d) clicking the toggle expands the in-flight panel", () => {
    const chip = renderLive([
      partialRecord("[Bash: pytest -q]", 1, "s1", "implement", 0, {
        tool_use_id: "tu_b1d",
        tool_detail: {
          kind: "tool_input", tool_name: "Bash",
          input: { command: "pytest -q tests/" }, truncated: false,
        },
      }),
    ]);
    const toggle = findOne(chip, "tool-marker-toggle");
    const panel = findOne(chip, "tool-marker-details");
    toggle.dispatch("click");
    assert.equal(panel.classList.contains("expanded"), true,
      "the toggle opens the panel");
  });

  // (B2) --------------------------------------------------------------------
  check("(B2) upgrading an in-flight chip leaves exactly one toggle and one panel", () => {
    const chip = renderLive([
      partialRecord("[Agent: description=self check, prompt=look at the diff]",
        1, "s1", "implement", 0,
        { tool_use_id: "tu_b2", tool_detail: agentInFlightDetail }),
      partialRecord("[Agent ✓ description=self check · No findings reported.]",
        2, "s1", "implement", 0, {
          tool_use_id: "tu_b2",
          is_error: false,
          tool_detail: {
            kind: "text",
            text: "No findings reported.",
            input: agentInput,
            truncated: false,
          },
        }),
    ]);
    assert.equal(chip.classList.contains("success"), true);
    assert.equal(findAll(chip, "tool-marker-toggle").length, 1,
      "attachChipDetail must replace, never duplicate, the toggle");
    assert.equal(findAll(chip, "tool-marker-details").length, 1,
      "attachChipDetail must replace, never duplicate, the panel");
    assert.notEqual(chipHeader(chip), "", "the settled header is not blank");
    const body = findOne(chip, "tool-marker-details-body");
    const text = body.textContent || "";
    assert.ok(text.includes("No findings reported."), "result text is shown");
    assert.ok(text.includes("look at the diff"),
      "the settled panel keeps the input too — never poorer than in-flight");
  });

  // (B3) --------------------------------------------------------------------
  check("(B3) tool_input renderer draws Bash as a `$ command` line", () => {
    const frag = app.renderToolDetailPanel({
      kind: "tool_input",
      tool_name: "Bash",
      input: { command: "pytest -q tests/engine", timeout: 600000 },
      truncated: false,
    });
    const host = document.createElement("div");
    host.appendChild(frag);
    const cmd = findOne(host, "tool-marker-bash-cmd");
    assert.ok(cmd, "Bash reuses the settled bash_output command styling");
    assert.ok((cmd.textContent || "").includes("pytest -q tests/engine"));
    // Non-command keys still show as a key/value block.
    const block = findOne(host, "tool-marker-input");
    assert.ok(block);
    assert.ok((block.textContent || "").includes("timeout"));
    assert.equal((block.textContent || "").includes("pytest -q"), false,
      "the command is not repeated in the key/value list");
  });

  check("(B3b) tool_input renderer draws a generic tool as a key/value list", () => {
    const frag = app.renderToolDetailPanel({
      kind: "tool_input",
      tool_name: "mcp__context7__get-library-docs",
      input: { library: "fastapi", tokens: 5000, opts: { deep: true } },
      truncated: false,
    });
    const host = document.createElement("div");
    host.appendChild(frag);
    const rows = findAll(host, "tool-marker-input-row");
    assert.equal(rows.length, 3, "one row per input key");
    const keys = findAll(host, "tool-marker-input-key").map((k) => k.textContent);
    assert.deepEqual(keys, ["library", "tokens", "opts"]);
    const text = host.textContent || "";
    assert.ok(text.includes("fastapi"));
    assert.ok(text.includes("5000"));
    assert.ok(text.includes("deep"), "non-string values are JSON-rendered");
  });

  check("(B3c) long / multi-line values go into a <pre>", () => {
    const frag = app.renderToolDetailPanel({
      kind: "tool_input",
      tool_name: "Agent",
      input: { description: "short", prompt: "line one\nline two" },
      truncated: false,
    });
    const host = document.createElement("div");
    host.appendChild(frag);
    const pres = findAll(host, "tool-marker-input-pre");
    assert.equal(pres.length, 1, "only the multi-line value gets a <pre>");
    assert.equal(pres[0].tagName, "PRE");
    assert.ok((pres[0].textContent || "").includes("line two"));
    const inline = findAll(host, "tool-marker-input-value");
    assert.equal(inline.length, 1);
    assert.equal(inline[0].textContent, "short");
  });

  check("(B3d) a truncated tool_input payload shows the truncation notice", () => {
    const frag = app.renderToolDetailPanel({
      kind: "tool_input",
      tool_name: "Agent",
      input: { prompt: "cut here" },
      truncated: true,
    });
    const host = document.createElement("div");
    host.appendChild(frag);
    assert.ok(findOne(host, "diff-truncated"),
      "reuses the existing tool.detail.truncated notice");
  });

  check("(B3e) an empty tool_input payload still renders without throwing", () => {
    const frag = app.renderToolDetailPanel({
      kind: "tool_input", tool_name: "unknown", input: {}, truncated: false,
    });
    const host = document.createElement("div");
    host.appendChild(frag);
    assert.ok(findOne(host, "tool-marker-input"));
    assert.ok(findOne(host, "tool-detail-empty"));
  });

  // (B4) --------------------------------------------------------------------
  check("(B4) text renderer shows the input block before the result", () => {
    const frag = app.renderToolDetailPanel({
      kind: "text",
      text: "No findings reported.",
      input: { description: "self check", prompt: "look" },
      truncated: false,
    });
    const host = document.createElement("div");
    host.appendChild(frag);
    const block = findOne(host, "tool-marker-input");
    const pre = findOne(host, "tool-marker-text-pre");
    assert.ok(block, "settled generic payload shows what was asked");
    assert.ok(pre, "…and what came back");
    assert.equal(pre.textContent, "No findings reported.");
    const wrap = findOne(host, "tool-marker-text-plain");
    const order = wrap.childNodes.map((c) => c.className || "");
    assert.ok(order.indexOf("tool-marker-input") < order.indexOf("tool-marker-text-pre"),
      "input comes first");
  });

  check("(B4b) a legacy text payload with no input renders exactly as before", () => {
    const frag = app.renderToolDetailPanel({
      kind: "text", text: "result only", truncated: false,
    });
    const host = document.createElement("div");
    host.appendChild(frag);
    assert.equal(findOne(host, "tool-marker-input"), null,
      "old jsonl has no input key — no empty block is invented for it");
    assert.equal(findOne(host, "tool-marker-text-pre").textContent, "result only");
  });

  check("(B4c) an empty input dict on a text payload adds no block", () => {
    const frag = app.renderToolDetailPanel({
      kind: "text", text: "r", input: {}, truncated: false,
    });
    const host = document.createElement("div");
    host.appendChild(frag);
    assert.equal(findOne(host, "tool-marker-input"), null);
  });

  // (B5) --------------------------------------------------------------------
  check("(B5) extractAssistantChipEvents gives an unsettled tool_use a detail", () => {
    // A step that ended while a tool call was still running: the final view is
    // rebuilt from raw_json, and must be no poorer than the live stream was.
    const events = app.extractAssistantChipEvents([
      {
        type: "assistant",
        message: {
          content: [{
            type: "tool_use", id: "tu_b5", name: "Agent", input: agentInput,
          }],
        },
      },
    ]);
    const chip = events.filter((e) => e.kind === "chip")[0];
    assert.ok(chip);
    assert.equal(chip.status, "in-flight");
    assert.ok(chip.detail, "an unsettled call must still be expandable");
    assert.equal(chip.detail.kind, "tool_input");
    assert.equal(chip.detail.tool_name, "Agent");
    assert.equal(chip.detail.input.prompt, agentInput.prompt);
    assert.equal(chip.detail.truncated, false);
  });

  check("(B5b) renderChipEvents attaches the in-flight detail panel", () => {
    const nodes = app.renderChipEvents([{
      kind: "chip",
      toolUseId: "tu_b5b",
      name: "Agent",
      status: "in-flight",
      header: "description=self check",
      detail: agentInFlightDetail,
    }]);
    const host = document.createElement("div");
    for (const n of nodes) host.appendChild(n);
    const chip = findOne(host, "tool-marker");
    assert.equal(chip.classList.contains("in-flight"), true);
    const panel = findOne(chip, "tool-marker-details");
    assert.ok(panel, "the final view can expand a never-settled call");
    assert.equal(panel.classList.contains("folded"), true);
    assert.equal(findAll(chip, "tool-marker-toggle").length, 1);
  });

  check("(B5c) a huge string in a raw_json tool_use is cut at the shared cap", () => {
    const huge = "z".repeat(20050);
    const events = app.extractAssistantChipEvents([
      {
        type: "assistant",
        message: {
          content: [{
            type: "tool_use", id: "tu_b5c", name: "Agent",
            input: { prompt: huge },
          }],
        },
      },
    ]);
    const chip = events.filter((e) => e.kind === "chip")[0];
    assert.equal(chip.detail.truncated, true);
    assert.equal(chip.detail.input.prompt.length, 20000,
      "same TOOL_DETAIL_PAYLOAD_MAX_CHARS boundary as the Python side");
  });

  // (B6) --------------------------------------------------------------------
  check("(B6) an unregistered tool's terminal detail carries `input`", () => {
    const events = app.extractAssistantChipEvents([
      {
        type: "assistant",
        message: {
          content: [{
            type: "tool_use", id: "tu_b6", name: "Agent", input: agentInput,
          }],
        },
      },
      {
        type: "user",
        message: {
          content: [{
            type: "tool_result", tool_use_id: "tu_b6",
            content: "No findings reported.", is_error: false,
          }],
        },
      },
    ]);
    const chip = events.filter((e) => e.kind === "chip")[0];
    assert.equal(chip.status, "success");
    assert.equal(chip.detail.kind, "text");
    assert.equal(chip.detail.text, "No findings reported.");
    assert.deepEqual(chip.detail.input, agentInput,
      "mirrors the backend `_build_generic_text_detail`");
  });

  check("(B6b) a registered tool's terminal detail keeps its own shape", () => {
    const events = app.extractAssistantChipEvents([
      {
        type: "assistant",
        message: {
          content: [{
            type: "tool_use", id: "tu_b6b", name: "Bash",
            input: { command: "ls -la" },
          }],
        },
      },
      {
        type: "user",
        message: {
          content: [{
            type: "tool_result", tool_use_id: "tu_b6b",
            content: "a\nb", is_error: false,
          }],
        },
      },
    ]);
    const chip = events.filter((e) => e.kind === "chip")[0];
    assert.equal(chip.detail.kind, "bash_output");
    assert.equal(chip.detail.command, "ls -la");
    assert.equal(chip.detail.input, undefined,
      "registered payload structures are untouched");
  });

  // (B7) --------------------------------------------------------------------
  check("(B7) a tool_detail-bearing record is in-flight iff is_error is absent", () => {
    // INVARIANT: `is_error` — not the presence of a detail payload — is what
    // separates the two chip states. Both records below carry a tool_detail.
    const container = document.createElement("div");
    app.renderConversation(container, [
      partialRecord("[Agent: prompt=a]", 1, "s1", "implement", 0,
        { tool_use_id: "tu_b7a", tool_detail: agentInFlightDetail }),
      partialRecord("[Agent ✗ prompt=a · InputValidationError]", 2, "s1",
        "implement", 0, {
          tool_use_id: "tu_b7b",
          is_error: true,
          tool_detail: { kind: "text", text: "InputValidationError", input: { prompt: "a" }, truncated: false },
        }),
    ], false);
    const chips = findAll(container, "tool-marker");
    assert.equal(chips.length, 2);
    assert.equal(chips[0].classList.contains("in-flight"), true);
    assert.equal(chips[1].classList.contains("failure"), true);
    // The failure panel opens by default; the in-flight one does not.
    assert.equal(findOne(chips[0], "tool-marker-details").classList.contains("folded"), true);
    assert.equal(findOne(chips[1], "tool-marker-details").classList.contains("expanded"), true);
  });
}
