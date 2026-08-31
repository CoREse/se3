/*
 * Lazy tool-call details in the web console.
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub is
 * installed. Exposes `registerHistoryLazyDetailTests({app, check, checkAsync,
 * findOne, findAll})`.
 *
 * The change under test: `GET /api/history/{flow_id}` now ships COLLAPSED-STATE
 * fields only. A successful tool call's body is stripped server-side and
 * fetched on expand; the chip header must still be byte-identical, the fetched
 * body must be cached, an unreachable body must say so, and a FAILED call must
 * keep behaving exactly as before (body inline, chip auto-expanded, no request).
 *
 * Coverage:
 *   (L1) rehydrateElidedValue preserves the line count and the leading chars
 *   (L2) an elided raw_json record renders the SAME header as the full one
 *   (L3) a lazified chip builds no panel body until it is expanded
 *   (L4) expanding fetches once, renders the body, and caches the result
 *   (L5) a second chip for the same call reuses the cache (no second request)
 *   (L6) an unreachable body renders the i18n "unavailable" line, not a blank
 *   (L7) a failed call stays inline + auto-expanded and issues NO request
 *   (L8) a stream_progress chip asks for the progress source, raw_json for raw
 *   (L9) an inline (unsummarized) live frame still renders its panel eagerly
 *   (L16) a marker-shaped value inside an UNMARKED record is left untouched
 *   (L17) an elided orphan tool_result fetches its original body on expand
 *   (L18) a new bundle generation retires the bodies cached against the old one
 *   (L19) a LIST tool input round-trips as a list, so the panel is unchanged
 *   (L20) a reply resolved under a superseded generation is never painted
 *   (L21) a DEEPLY nested stub is still found and restored by "View raw"
 */
import assert from "node:assert/strict";

export async function registerHistoryLazyDetailTests(ctx) {
  const { app, check, checkAsync, findOne, findAll } = ctx;

  const FLOW = "flow-lazy";
  const BIG = Array.from({ length: 300 }, (_, i) => `line ${i} of output`).join("\n");
  const BIG_FILE = Array.from({ length: 200 }, (_, i) => `def f${i}(): return ${i}`)
    .join("\n");

  // Mirror of the server's `elide_string` (history_summary.py). Head width is
  // asserted against the JS side's only requirement: the header formatters read
  // at most 80 characters (60 on any path a successful call can take) plus the
  // line count, so the head must stay wider than every preview window.
  const HEAD = 96;
  const elide = (text) => ({
    __elided__: true,
    head: text.slice(0, HEAD),
    lines: text.split("\n").length,
    chars: text.length,
  });

  const progressRecord = (extras) => ({
    step_id: extras.step_id || "s1",
    step_type: "implement",
    ordinal: extras.ordinal || 0,
    message: Object.assign({
      type: "stream_progress",
      role: "assistant",
      partial: true,
      timestamp: extras.ordinal || 0,
    }, extras.message),
  });

  const assistantRecord = (blocks, message) => ({
    step_id: "s1",
    step_type: "implement",
    ordinal: 9,
    message: Object.assign({
      role: "assistant",
      content: "",
      timestamp: 9,
      raw_json: [{ type: "assistant", message: { content: blocks } }],
    }, message || {}),
  });

  const chipHeader = (chip) => {
    const d = findOne(chip, "tool-marker-detail");
    return d ? d.textContent : "";
  };
  const renderOne = (records) => {
    const container = document.createElement("div");
    app.renderConversation(container, records, false);
    return container;
  };

  // A fetch stub that records every URL and answers from a canned table.
  let calls = [];
  const savedFetch = globalThis.fetch;
  const installFetch = (handler) => {
    calls = [];
    globalThis.fetch = (url) => {
      calls.push(String(url));
      return Promise.resolve(handler(String(url)));
    };
  };
  const okJson = (payload) => ({
    ok: true, status: 200, json: () => Promise.resolve(payload),
  });
  const httpError = (status) => ({
    ok: false, status, json: () => Promise.resolve({}),
  });
  // Let the fetch promise chain inside attachChipDetail settle.
  const settle = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };

  // (L1) ---------------------------------------------------------------------
  check("(L1) rehydrateElidedValue keeps the line count and the leading chars", () => {
    const stub = elide(BIG);
    const back = app.rehydrateElidedValue(stub);
    assert.equal(typeof back, "string");
    assert.equal(back.split("\n").length, BIG.split("\n").length,
      "line count must survive — Write/Edit/Read headers are line counts");
    assert.equal(back.slice(0, HEAD), BIG.slice(0, HEAD),
      "the leading chars must survive — Bash/Grep headers are previews");
    // Nested values are rehydrated in place; plain values pass through.
    const nested = app.rehydrateElidedValue({ a: [stub], b: "plain", c: 3 });
    assert.equal(nested.a[0].split("\n").length, BIG.split("\n").length);
    assert.equal(nested.b, "plain");
    assert.equal(nested.c, 3);
    // A single-line stub with no `lines` still becomes a usable string.
    assert.equal(app.rehydrateElidedValue({ __elided__: true, head: "x" }), "x");
    assert.equal(app.isElidedStub({ __elided__: true }), true);
    assert.equal(app.isElidedStub({ head: "x" }), false);
  });

  // (L2) ---------------------------------------------------------------------
  check("(L2) an elided record renders the SAME chip header as the full one", () => {
    const blocks = (content, result) => [
      { type: "tool_use", id: "tu_w", name: "Write",
        input: { file_path: "src/x.py", content: content } },
      { type: "tool_result", tool_use_id: "tu_w", content: result },
      { type: "tool_use", id: "tu_b", name: "Bash", input: { command: BIG } },
      { type: "tool_result", tool_use_id: "tu_b", content: result },
    ];
    const full = renderOne([assistantRecord(blocks(BIG_FILE, BIG))]);
    const lazy = renderOne([assistantRecord(
      blocks(elide(BIG_FILE), elide(BIG)),
      { lazy_tool_use_ids: ["tu_w", "tu_b"], detail_flow: FLOW },
    )]);
    const fullChips = findAll(full, "tool-marker");
    const lazyChips = findAll(lazy, "tool-marker");
    assert.equal(fullChips.length, lazyChips.length);
    assert.ok(fullChips.length >= 2, "both tool calls render a chip");
    for (let i = 0; i < fullChips.length; i++) {
      assert.equal(chipHeader(lazyChips[i]), chipHeader(fullChips[i]),
        `chip ${i} header must be byte-identical to the un-summarized render`);
      assert.equal(lazyChips[i].classList.contains("success"),
        fullChips[i].classList.contains("success"));
    }
  });

  // (L3) + (L4) --------------------------------------------------------------
  await checkAsync("(L3/L4) a lazy chip is empty until expanded, then fetched once", async () => {
    app.clearLazyDetailCache();
    installFetch(() => okJson({
      flow_id: FLOW, tool_use_id: "tu_r", source: "progress",
      detail: { kind: "read_text", file_path: "a.py", text: "alpha\nbeta" },
    }));
    const container = renderOne([
      progressRecord({ ordinal: 1, message: {
        content: "[Read: a.py]", tool_use_id: "tu_r",
      } }),
      progressRecord({ ordinal: 2, message: {
        content: "[Read ✓ a.py · 2 lines]",
        tool_use_id: "tu_r",
        is_error: false,
        tool_detail_lazy: true,
        detail_flow: FLOW,
      } }),
    ]);
    const chip = findAll(container, "tool-marker")[0];
    assert.ok(chip, "the settled chip exists");
    const panel = findOne(chip, "tool-marker-details");
    assert.ok(panel, "a lazy chip still offers a details panel");
    assert.equal(panel.classList.contains("folded"), true);
    const body = findOne(panel, "tool-marker-details-body");
    assert.equal(body.childNodes.length, 0,
      "collapsed lazy chip builds no panel DOM — that is the render saving");
    assert.equal(calls.length, 0, "and issues no request while collapsed");

    const toggle = findOne(chip, "tool-marker-toggle");
    toggle.dispatch("click");
    assert.equal(panel.classList.contains("expanded"), true);
    await settle();
    assert.equal(calls.length, 1, "expanding fetches exactly once");
    assert.match(calls[0], /^\/api\/history\/flow-lazy\/detail\?/);
    assert.match(calls[0], /tool_use_id=tu_r/);
    assert.match(calls[0], /source=progress/);
    // The record address rides along: `tool_use_id` alone repeats across a flow.
    assert.match(calls[0], /step_id=s1/);
    assert.match(calls[0], /ordinal=2/);
    assert.ok(body.textContent.includes("alpha"),
      "the fetched body is what the panel renders");

    // Collapse + re-expand: served from the frontend cache, no second request.
    toggle.dispatch("click");
    toggle.dispatch("click");
    await settle();
    assert.equal(calls.length, 1, "re-expanding never re-requests");
  });

  // (L5) ---------------------------------------------------------------------
  await checkAsync("(L5) two chips for one call share a single request", async () => {
    app.clearLazyDetailCache();
    installFetch(() => okJson({
      source: "progress", detail: { kind: "text", text: "shared body" },
    }));
    const mk = () => renderOne([progressRecord({ ordinal: 1, message: {
      content: "[Bash ✓ ls]", tool_use_id: "tu_dup", is_error: false,
      tool_detail_lazy: true, detail_flow: FLOW,
    } })]);
    const a = mk();
    const b = mk();
    findOne(findAll(a, "tool-marker")[0], "tool-marker-toggle").dispatch("click");
    findOne(findAll(b, "tool-marker")[0], "tool-marker-toggle").dispatch("click");
    await settle();
    assert.equal(calls.length, 1,
      "the console view and the history view of one call dedupe to one request");
    for (const c of [a, b]) {
      assert.ok(findOne(c, "tool-marker-details-body").textContent
        .includes("shared body"));
    }
  });

  // (L6) ---------------------------------------------------------------------
  await checkAsync("(L6) an unreachable body renders the i18n unavailable line", async () => {
    app.clearLazyDetailCache();
    installFetch(() => httpError(503));
    const container = renderOne([progressRecord({ ordinal: 1, message: {
      content: "[Bash ✓ ls]", tool_use_id: "tu_down", is_error: false,
      tool_detail_lazy: true, detail_flow: FLOW,
    } })]);
    const chip = findAll(container, "tool-marker")[0];
    const body = findOne(chip, "tool-marker-details-body");
    findOne(chip, "tool-marker-toggle").dispatch("click");
    await settle();
    const notice = findOne(body, "tool-detail-unavailable");
    assert.ok(notice, "the panel must never stay blank or spinning");
    assert.ok(notice.textContent.length > 0);
    assert.equal(findOne(body, "tool-detail-loading"), null,
      "the loading line is replaced, not left behind");
    // A failure is not cached: collapsing and re-expanding retries.
    findOne(chip, "tool-marker-toggle").dispatch("click");
    findOne(chip, "tool-marker-toggle").dispatch("click");
    await settle();
    assert.equal(calls.length, 2, "the unavailable state is retryable");
  });

  // (L7) ---------------------------------------------------------------------
  await checkAsync("(L7) a failed call stays inline, auto-expanded, and silent", async () => {
    app.clearLazyDetailCache();
    installFetch(() => okJson({ source: "progress", detail: { kind: "text", text: "x" } }));
    const container = renderOne([
      progressRecord({ ordinal: 1, message: {
        content: "[Bash ✗ boom]", tool_use_id: "tu_err", is_error: true,
        tool_detail: { kind: "text", text: "ENOENT: no such file" },
      } }),
      // A whole session of failures must not add up to a burst of requests.
      progressRecord({ ordinal: 2, message: {
        content: "[Edit ✗ nope]", tool_use_id: "tu_err2", is_error: true,
        tool_detail: { kind: "text", text: "second failure" },
      } }),
    ]);
    await settle();
    const chips = findAll(container, "tool-marker");
    assert.equal(chips.length, 2);
    for (const chip of chips) {
      assert.equal(chip.classList.contains("failure"), true);
      const panel = findOne(chip, "tool-marker-details");
      assert.equal(panel.classList.contains("expanded"), true,
        "failure details stay default-expanded");
      assert.ok(findOne(panel, "tool-marker-details-body").textContent.length > 0,
        "and their body is rendered inline, not fetched");
    }
    assert.equal(calls.length, 0,
      "loading a failure-heavy session issues zero on-demand requests");
  });

  // (L8) ---------------------------------------------------------------------
  await checkAsync("(L8) a raw_json chip asks for the raw source and renders it", async () => {
    app.clearLazyDetailCache();
    installFetch(() => okJson({
      source: "raw", tool_name: "Write", status: "success",
      input: { file_path: "src/x.py", content: "one\ntwo\nthree" },
      result: "written",
    }));
    const container = renderOne([assistantRecord([
      { type: "tool_use", id: "tu_raw", name: "Write",
        input: { file_path: "src/x.py", content: elide(BIG_FILE) } },
      { type: "tool_result", tool_use_id: "tu_raw", content: "written" },
    ], { lazy_tool_use_ids: ["tu_raw"], detail_flow: FLOW })]);
    const chip = findAll(container, "tool-marker")[0];
    const body = findOne(chip, "tool-marker-details-body");
    assert.equal(body.childNodes.length, 0);
    findOne(chip, "tool-marker-toggle").dispatch("click");
    await settle();
    assert.equal(calls.length, 1);
    assert.match(calls[0], /source=raw/);
    assert.match(calls[0], /tool_use_id=tu_raw/);
    // The panel is built by the SAME local formatter an un-summarized bundle
    // would have used, so the expanded view matches the old one exactly.
    assert.ok(body.textContent.includes("two"), body.textContent.slice(0, 200));
  });

  // (L9) ---------------------------------------------------------------------
  check("(L9) a live (unsummarized) frame still renders its panel eagerly", () => {
    installFetch(() => okJson({}));
    const container = renderOne([progressRecord({ ordinal: 1, message: {
      content: "[Read ✓ a.py · 2 lines]", tool_use_id: "tu_live",
      is_error: false,
      tool_detail: { kind: "read_text", file_path: "a.py", text: "live\nbody" },
    } })]);
    const chip = findAll(container, "tool-marker")[0];
    const panel = findOne(chip, "tool-marker-details");
    assert.equal(panel.classList.contains("folded"), true);
    assert.ok(findOne(panel, "tool-marker-details-body").textContent.includes("live"),
      "the realtime WebSocket path is untouched — its bodies still ride inline");
    assert.equal(calls.length, 0);
  });

  // Unit checks on the helpers the above drive through the DOM.
  check("(L10) lazy ref helpers refuse an unaddressable chip", () => {
    const full = { flow: FLOW, stepId: "s1", ordinal: 2, toolUseId: "tu" };
    assert.equal(app.isLazyDetail(app.makeLazyDetail(full)), true);
    assert.equal(app.isLazyDetail({ kind: "text" }), false);
    // Every part of the address is required: a ref missing one of them would
    // fetch a body the server cannot identify, or the WRONG record's body.
    for (const missing of ["flow", "stepId", "ordinal", "toolUseId"]) {
      const ref = Object.assign({}, full);
      delete ref[missing];
      assert.equal(app.makeLazyDetail(ref), null, missing);
    }
    assert.equal(app.makeLazyDetail(Object.assign({}, full, { ordinal: -1 })), null);
    assert.equal(
      app.lazyDetailUrl({ flow: "a/b", stepId: "01_x", ordinal: 7,
                          toolUseId: "t u", source: "raw" }),
      "/api/history/a%2Fb/detail?tool_use_id=t%20u&step_id=01_x&ordinal=7&source=raw",
    );
    // Two chips of one flow that share a synthesized id are distinct cache
    // entries — the address, not the id, is the identity.
    assert.notEqual(
      app.lazyDetailCacheKey(app.makeLazyDetail(
        { flow: FLOW, stepId: "s1", ordinal: 1, toolUseId: "codex_tool_1" })),
      app.lazyDetailCacheKey(app.makeLazyDetail(
        { flow: FLOW, stepId: "s2", ordinal: 1, toolUseId: "codex_tool_1" })),
    );
    // chipDetailForFragment prefers an inline body and only then the lazy ref.
    const inline = { toolDetail: { kind: "text" }, toolDetailLazy: true,
      detailFlow: FLOW, stepId: "s1", ordinal: 1, toolUseId: "tu" };
    assert.equal(app.chipDetailForFragment(inline).kind, "text");
    const lazyOnly = { toolDetail: null, toolDetailLazy: true,
      detailFlow: FLOW, stepId: "s1", ordinal: 1, toolUseId: "tu" };
    assert.equal(app.isLazyDetail(app.chipDetailForFragment(lazyOnly)), true);
    assert.equal(app.chipDetailForFragment({ toolDetail: null }), null);
    assert.equal(app.lazyOptsForRecord({ detailFlow: FLOW, lazyToolUseIds: [] }), null);
    assert.deepEqual(
      app.lazyOptsForRecord({ detailFlow: FLOW, stepId: "s1", ordinal: 4,
                              lazyToolUseIds: ["a"], lazyBodyMask: "r",
                              detailVersion: "v1" }),
      { flow: FLOW, stepId: "s1", ordinal: 4, toolUseIds: ["a"],
        bodyMask: "r", version: "v1" },
    );
    // The record VERSION enters the cache key: a retry rewriting a line in
    // place keeps its address, so without it a body cached for the previous
    // attempt would be served under the replacement chip.
    assert.notEqual(
      app.lazyDetailCacheKey(app.makeLazyDetail(
        { flow: FLOW, stepId: "s1", ordinal: 1, toolUseId: "tu", version: "v1" })),
      app.lazyDetailCacheKey(app.makeLazyDetail(
        { flow: FLOW, stepId: "s1", ordinal: 1, toolUseId: "tu", version: "v2" })),
    );
  });

  // The raw restore runs a longer promise chain than a chip expand does.
  const settleRestore = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };

  // (L11) --------------------------------------------------------------------
  await checkAsync("(L11) View raw restores the bodies the summary held back", async () => {
    app.clearLazyDetailCache();
    installFetch(() => okJson({
      source: "raw", tool_name: "Write", status: "success",
      input: { file_path: "src/x.py", content: BIG_FILE },
      result: "written",
    }));
    const norm = app.normalizeRecord(assistantRecord([
      { type: "tool_use", id: "tu_raw", name: "Write",
        input: { file_path: "src/x.py", content: elide(BIG_FILE) } },
      { type: "tool_result", tool_use_id: "tu_raw", content: "written" },
    ], { content: "done", lazy_tool_use_ids: ["tu_raw"], detail_flow: FLOW }));

    const wrap = app.makeAssistantRawToggle("done", norm);
    const pre = findOne(wrap, "raw-json");
    findOne(wrap, "raw-toggle").dispatch("click");
    await settleRestore();
    assert.equal(calls.length, 1, "View raw fetches the held-back body");
    assert.match(calls[0], /source=raw/);
    assert.ok(pre.textContent.includes(BIG_FILE.split("\n")[0]),
      "the ORIGINAL content is what View raw shows");
    assert.ok(!pre.textContent.includes("__elided__"),
      "no elision stub may survive into the raw view");
    assert.ok(!pre.textContent.includes("lazy_tool_use_ids"),
      "and neither may the wire-only markers the shaping added");

    // The chip panel for the same call is served from the same cache.
    const container = renderOne([assistantRecord([
      { type: "tool_use", id: "tu_raw", name: "Write",
        input: { file_path: "src/x.py", content: elide(BIG_FILE) } },
      { type: "tool_result", tool_use_id: "tu_raw", content: "written" },
    ], { lazy_tool_use_ids: ["tu_raw"], detail_flow: FLOW })]);
    const chip = findAll(container, "tool-marker")[0];
    findOne(chip, "tool-marker-toggle").dispatch("click");
    await settleRestore();
    assert.equal(calls.length, 1,
      "the panel and the raw view share one on-demand request");
  });

  // (L12) --------------------------------------------------------------------
  await checkAsync("(L12) View raw restores a lazified stream_progress record", async () => {
    app.clearLazyDetailCache();
    installFetch(() => okJson({
      source: "progress",
      detail: { kind: "read_text", file_path: "a.py", text: "alpha\nbeta" },
    }));
    const norm = app.normalizeRecord(progressRecord({ ordinal: 1, message: {
      content: "[Read ✓ a.py · 2 lines]", tool_use_id: "tu_p", is_error: false,
      tool_detail_lazy: true, detail_flow: FLOW,
    } }));
    const wrap = app.makeUserRawToggle(norm);
    const pre = findOne(wrap, "raw-json");
    findOne(wrap, "raw-toggle").dispatch("click");
    await settleRestore();
    assert.equal(calls.length, 1);
    assert.match(calls[0], /source=progress/);
    assert.ok(pre.textContent.includes("alpha"),
      "the dropped tool_detail is put back before the record is printed");
    assert.ok(!pre.textContent.includes("tool_detail_lazy"),
      "the marker it replaced is gone");
  });

  // (L13) --------------------------------------------------------------------
  await checkAsync("(L13) an unreachable raw body says so and stays retryable", async () => {
    app.clearLazyDetailCache();
    installFetch(() => httpError(503));
    const norm = app.normalizeRecord(assistantRecord([
      { type: "tool_use", id: "tu_gone", name: "Write",
        input: { file_path: "src/x.py", content: elide(BIG_FILE) } },
    ], { content: "done", lazy_tool_use_ids: ["tu_gone"], detail_flow: FLOW }));
    const wrap = app.makeAssistantRawToggle("done", norm);
    const pre = findOne(wrap, "raw-json");
    const btn = findOne(wrap, "raw-toggle");
    btn.dispatch("click");
    await settleRestore();
    assert.equal(calls.length, 1);
    assert.ok(pre.textContent.length > 0, "never blank, never left spinning");
    assert.ok(!pre.textContent.includes("Loading"),
      "the loading line is replaced by the unavailable notice");
    // Hide + show again: a failed restore is a genuine retry, like a chip's.
    btn.dispatch("click");
    btn.dispatch("click");
    await settleRestore();
    assert.equal(calls.length, 2);
  });

  // (L14) --------------------------------------------------------------------
  check("(L14) raw-restore helpers only fire for a payload that lost something", () => {
    const plain = [{ type: "assistant", message: { content: [
      { type: "tool_use", id: "tu", name: "Bash", input: { command: "ls" } },
    ] } }];
    assert.equal(app.containsElidedStub(plain), false);
    assert.equal(app.lazyRawRefsFor({ detailFlow: FLOW }, plain), null,
      "a record that kept everything must not fetch anything");
    const lazy = [{ type: "assistant", message: { content: [
      { type: "tool_use", id: "tu", name: "Write",
        input: { file_path: "x.py", content: elide(BIG_FILE) } },
    ] } }];
    assert.equal(app.containsElidedStub(lazy), true);
    const refs = app.lazyRawRefsFor({ detailFlow: FLOW }, lazy);
    assert.deepEqual(Array.from(refs.raw), ["tu"]);
    assert.equal(refs.flow, FLOW);
    // Unaddressable (no flow id) degrades to printing what is held, no fetch.
    assert.equal(app.lazyRawRefsFor({ detailFlow: "" }, lazy), null);
  });

  // (L15) --------------------------------------------------------------------
  await checkAsync("(L15) one id in two records is two fetches, and a full replace drops them",
    async () => {
      app.clearLazyDetailCache();
      installFetch((url) => okJson({
        source: "progress",
        detail: { kind: "text", text: /step_id=s1/.test(url) ? "FIRST" : "SECOND" },
      }));
      // codex synthesizes `codex_tool_1` per call, so the SAME id can name a
      // call in two different STEPS of one flow. Each chip must show its own
      // body — a flow-wide id lookup used to hand both the same one.
      const container = renderOne([
        progressRecord({ ordinal: 1, step_id: "s1", message: {
          content: "[Bash ✓ one]", tool_use_id: "codex_tool_1", is_error: false,
          tool_detail_lazy: true, detail_flow: FLOW,
        } }),
        progressRecord({ ordinal: 1, step_id: "s2", message: {
          content: "[Bash ✓ two]", tool_use_id: "codex_tool_1", is_error: false,
          tool_detail_lazy: true, detail_flow: FLOW,
        } }),
      ]);
      const chips = findAll(container, "tool-marker");
      assert.equal(chips.length, 2);
      for (const chip of chips) {
        findOne(chip, "tool-marker-toggle").dispatch("click");
      }
      await settle();
      assert.equal(calls.length, 2,
        "a shared tool_use_id must not collapse two messages into one entry");
      const bodies = chips.map(
        (c) => findOne(c, "tool-marker-details-body").textContent);
      assert.ok(bodies[0].includes("FIRST"), bodies[0]);
      assert.ok(bodies[1].includes("SECOND"), bodies[1]);

      // A full replace re-generates the bundle, so its cached bodies go with it
      // — but only that flow's.
      app.dropLazyDetailCacheForFlow("some-other-flow");
      const again = renderOne([progressRecord({ ordinal: 1, step_id: "s1", message: {
        content: "[Bash ✓ one]", tool_use_id: "codex_tool_1", is_error: false,
        tool_detail_lazy: true, detail_flow: FLOW,
      } })]);
      findOne(findAll(again, "tool-marker")[0], "tool-marker-toggle").dispatch("click");
      await settle();
      assert.equal(calls.length, 2, "another flow's eviction must not touch ours");
      app.dropLazyDetailCacheForFlow(FLOW);
      const fresh = renderOne([progressRecord({ ordinal: 1, step_id: "s1", message: {
        content: "[Bash ✓ one]", tool_use_id: "codex_tool_1", is_error: false,
        tool_detail_lazy: true, detail_flow: FLOW,
      } })]);
      findOne(findAll(fresh, "tool-marker")[0], "tool-marker-toggle").dispatch("click");
      await settle();
      assert.equal(calls.length, 3, "a replaced generation re-fetches its bodies");
    });

  // (L16) --------------------------------------------------------------------
  check("(L16) an unmarked record's marker-shaped value is left alone", () => {
    // `__elided__` is not reserved in a tool's own arguments. A live frame — or
    // any record the summary did not touch — is already whole, so rewriting a
    // marker-shaped value inside it would change both the folded header and the
    // expanded panel of a payload that lost nothing.
    const literal = { __elided__: true, head: "abc", lines: 2 };
    const blocks = [
      { type: "tool_use", id: "tu_lit", name: "Weird", input: { spec: literal } },
      { type: "tool_result", tool_use_id: "tu_lit", content: "ok" },
    ];
    const live = renderOne([assistantRecord(blocks)]);
    const events = app.extractAssistantChipEvents(
      [{ type: "assistant", message: { content: blocks } }], null);
    assert.deepEqual(events[0]._input.spec, literal,
      "an unmarked record keeps its payload structure");
    assert.equal(app.isLazyDetail(events[0].detail), false,
      "an unmarked chip derives its panel locally, with no request");
    // And the same blocks under a lazy marker DO get rehydrated.
    const marked = app.extractAssistantChipEvents(
      [{ type: "assistant", message: { content: blocks } }],
      { flow: FLOW, stepId: "s1", ordinal: 9, toolUseIds: ["tu_lit"] });
    assert.equal(typeof marked[0]._input.spec, "string");
    assert.ok(app.isLazyDetail(marked[0].detail));
    // The un-marked render still built a real panel (nothing to fetch).
    assert.ok(findOne(findAll(live, "tool-marker")[0], "tool-marker-details-body"));
  });

  // (L17) --------------------------------------------------------------------
  await checkAsync("(L17) an elided ORPHAN tool_result fetches its original body",
    async () => {
      app.clearLazyDetailCache();
      installFetch(() => okJson({
        source: "raw", tool_name: "Tool", input: {},
        result: "THE WHOLE ORPHAN RESULT", status: "success",
      }));
      // A legacy / mis-streamed record can carry a large tool_result with no
      // preceding tool_use. The summary strips it too, so expanding must show
      // the ORIGINAL result rather than the prefix the stub kept.
      const events = app.extractAssistantChipEvents(
        [{ type: "assistant", message: { content: [
          { type: "tool_result", tool_use_id: "tu_orphan", content: elide(BIG) },
        ] } }],
        { flow: FLOW, stepId: "s1", ordinal: 9, toolUseIds: ["tu_orphan"] },
      );
      assert.ok(app.isLazyDetail(events[0].detail),
        "an elided orphan result must not pass its stub off as the detail");
      const host = document.createElement("div");
      for (const n of app.renderChipEvents(events)) host.appendChild(n);
      const chip = findOne(host, "tool-marker");
      assert.ok(chip, "an orphan result still renders a chip");
      assert.equal(findOne(chip, "tool-marker-details-body").textContent, "",
        "no body is built before it is asked for");
      findOne(chip, "tool-marker-toggle").dispatch("click");
      await settle();
      assert.equal(calls.length, 1, "the orphan result must issue a request");
      const body = findOne(chip, "tool-marker-details-body").textContent;
      assert.ok(body.includes("THE WHOLE ORPHAN RESULT"), body);
      assert.ok(!body.includes("Loading"), body);
    });

  // (L18) --------------------------------------------------------------------
  await checkAsync("(L18) a REST full replacement retires the cached bodies",
    async () => {
      app.clearLazyDetailCache();
      let answer = "FIRST ATTEMPT";
      installFetch(() => okJson({
        source: "progress", detail: { kind: "text", text: answer },
      }));
      const record = () => progressRecord({ ordinal: 4, step_id: "s1", message: {
        content: "[Bash ✓ retryable]", tool_use_id: "tu_retry", is_error: false,
        tool_detail_lazy: true, detail_flow: FLOW,
      } });
      const open = async (container) => {
        findOne(findAll(container, "tool-marker")[0], "tool-marker-toggle")
          .dispatch("click");
        await settle();
        return findOne(findAll(container, "tool-marker")[0],
                       "tool-marker-details-body").textContent;
      };
      app.noteLazyDetailBundle(FLOW, 7, false);
      assert.ok((await open(renderOne([record()]))).includes("FIRST ATTEMPT"));
      assert.equal(calls.length, 1);

      // The browser was away while the daemon retried the step, rewriting the
      // SAME step_id#ordinal. It comes back to an authoritative REST snapshot —
      // a new bundle generation — so the body cached against that address must
      // not be reused.
      answer = "SECOND ATTEMPT";
      app.noteLazyDetailBundle(FLOW, 8, true);
      assert.ok((await open(renderOne([record()]))).includes("SECOND ATTEMPT"));
      assert.equal(calls.length, 2, "a new generation re-fetches");

      // A same-generation redelivery keeps the cache warm.
      app.noteLazyDetailBundle(FLOW, 8, false);
      assert.ok((await open(renderOne([record()]))).includes("SECOND ATTEMPT"));
      assert.equal(calls.length, 2, "an unchanged generation must not re-fetch");
    });

  // (L19) --------------------------------------------------------------------
  await checkAsync("(L19) a LIST input comes back as a list, panel unchanged",
    async () => {
      // A tool_use whose whole input IS an array is enumerated by index here
      // (`Object.keys` treats an array as an object), so the un-summarized
      // panel printed its entries. The lazy panel must print the same ones —
      // an `input: {}` reply would silently drop every argument.
      const entry = "ALPHA-ENTRY " + BIG;
      const full = renderOne([assistantRecord([
        { type: "tool_use", id: "tu_arr", name: "Weird", input: [entry, "tail"] },
        { type: "tool_result", tool_use_id: "tu_arr", content: "ok" },
      ])]);
      const fullBody = findOne(findAll(full, "tool-marker")[0],
                               "tool-marker-details-body").textContent;
      assert.ok(fullBody.includes("ALPHA-ENTRY"), fullBody.slice(0, 200));

      app.clearLazyDetailCache();
      installFetch(() => okJson({
        source: "raw", tool_name: "Weird", input: [entry, "tail"],
        result: "ok", status: "success",
      }));
      const lazy = renderOne([assistantRecord(
        [
          { type: "tool_use", id: "tu_arr", name: "Weird",
            input: [elide(entry), "tail"] },
          { type: "tool_result", tool_use_id: "tu_arr", content: "ok" },
        ],
        { lazy_tool_use_ids: ["tu_arr"], detail_flow: FLOW },
      )]);
      const chip = findAll(lazy, "tool-marker")[0];
      assert.equal(findOne(chip, "tool-marker-details-body").textContent, "",
        "no body before it is asked for");
      findOne(chip, "tool-marker-toggle").dispatch("click");
      await settle();
      assert.equal(calls.length, 1);
      assert.equal(findOne(chip, "tool-marker-details-body").textContent,
                   fullBody,
                   "the expanded panel must match the un-summarized one");
    });

  // (L20) --------------------------------------------------------------------
  await checkAsync("(L20) a reply resolved under a superseded generation is dropped",
    async () => {
      // INVARIANT: evicting the cache entry is not enough on its own. A consumer
      // already awaiting the old promise holds a reference the eviction cannot
      // reach, and a silent full replacement whose summarized record is visually
      // identical keeps the very DOM that continuation would paint into. So the
      // epoch is re-checked at resolve time and the stale answer is dropped.
      app.clearLazyDetailCache();
      let answer = "FIRST GENERATION BODY";
      const gate = [];
      calls = [];
      globalThis.fetch = (url) => {
        calls.push(String(url));
        const body = answer;
        return new Promise((resolve) => gate.push(
          () => resolve(okJson({
            source: "progress", detail: { kind: "text", text: body },
          }))));
      };
      const record = () => progressRecord({ ordinal: 1, step_id: "s1", message: {
        content: "[Bash ✓ one]", tool_use_id: "codex_tool_1", is_error: false,
        tool_detail_lazy: true, detail_flow: FLOW, detail_version: "v1",
      } });
      // The delivery that opened the session names the bundle generation the
      // body below is fetched under.
      app.noteLazyDetailBundle(FLOW, 1, false);
      const view = renderOne([record()]);
      const chip = findAll(view, "tool-marker")[0];
      findOne(chip, "tool-marker-toggle").dispatch("click");
      await settle();
      assert.equal(calls.length, 1, "the expand did not start a request");

      // The bundle is replaced while that request is still in flight.
      answer = "SECOND GENERATION BODY";
      app.noteLazyDetailBundle(FLOW, 2, /*replaced=*/false);
      gate.shift()();                      // the OLD generation's reply lands
      await settle();
      const body = findOne(chip, "tool-marker-details-body").textContent;
      assert.ok(!body.includes("FIRST GENERATION BODY"),
        "a superseded body was painted: " + body.slice(0, 120));
      // It was re-issued under the new generation instead of failing outright.
      assert.equal(calls.length, 2, "the superseded request was not re-issued");
      gate.shift()();
      await settle();
      assert.ok(
        findOne(chip, "tool-marker-details-body").textContent
          .includes("SECOND GENERATION BODY"),
        "the replacement generation's body never landed");
    });

  // (L21) --------------------------------------------------------------------
  await checkAsync("(L21) a deeply nested stub is still restored by View raw",
    async () => {
      // The server walks a tool input 32 container levels deep, so a restore
      // that gave up sooner printed the elision object as if it were the
      // original record and never asked for the body it was missing.
      app.clearLazyDetailCache();
      let deep = { text: BIG_FILE };
      let stubbed = { text: elide(BIG_FILE) };
      for (let i = 0; i < 16; i++) {
        deep = { nest: deep };
        stubbed = { nest: stubbed };
      }
      installFetch(() => okJson({
        source: "raw", tool_name: "Weird", status: "success",
        input: { spec: deep }, result: "ok",
      }));
      const norm = app.normalizeRecord(assistantRecord(
        [
          { type: "tool_use", id: "tu_deep", name: "Weird",
            input: { spec: stubbed } },
          { type: "tool_result", tool_use_id: "tu_deep", content: "ok" },
        ],
        { lazy_tool_use_ids: ["tu_deep"], detail_flow: FLOW,
          detail_version: "v1" },
      ));
      const payload = norm.raw.raw_json;
      assert.ok(app.containsElidedStub(payload, 0),
        "the fixture no longer nests the stub out of reach");
      const refs = app.lazyRawRefsFor(norm, payload);
      assert.ok(refs && refs.raw.has("tu_deep"),
        "a deeply nested stub went unnoticed by the raw restore");
      const pre = document.createElement("pre");
      const painted = app.paintRawInto(pre, norm, payload, null);
      await settleRestore();
      assert.equal(calls.length, 1, "View raw made no detail request");
      assert.equal(await painted, true);
      assert.ok(pre.textContent.includes("def f0(): return 0"),
        "the original body was never spliced back in");
      assert.ok(!pre.textContent.includes("__elided__"),
        "the raw view still printed the summary's stub");
    });

  // (L22) --------------------------------------------------------------------
  check("(L22) only the BODY the server stripped is rehydrated", () => {
    // A call can be lazy for one body while the other rides inline: boundary
    // rule (c) keeps a fold-visible tool_use input whole even when the record's
    // envelope tool_result is collapsed. `lazy_tool_use_ids` names the CALL, so
    // reading it as "both bodies were replaced" rewrote an input the server had
    // deliberately kept -- and `__elided__` is legal in a tool's real arguments,
    // so that rewrite lands on a live payload and changes the folded header.
    const literal = { __elided__: true, head: "abc", lines: 2, chars: 3 };
    const blocks = [
      { type: "tool_use", id: "tu_mix", name: "Weird", input: { spec: literal } },
      { type: "tool_result", tool_use_id: "tu_mix", content: elide(BIG) },
    ];
    const lazy = { flow: FLOW, stepId: "s1", ordinal: 9,
                   toolUseIds: ["tu_mix"], bodyMask: "r" };
    const events = app.extractAssistantChipEvents(
      [{ type: "assistant", message: { content: blocks } }], lazy);
    assert.deepEqual(events[0]._input.spec, literal,
      "an inline input was rebuilt from its marker shape");
    assert.ok(app.isLazyDetail(events[0].detail),
      "the stripped RESULT must still fetch its body on expand");
    // ...and the mirror: input stripped, result inline.
    const other = [
      { type: "tool_use", id: "tu_mix2", name: "Weird", input: elide(BIG) },
      { type: "tool_result", tool_use_id: "tu_mix2", content: { spec: literal } },
    ];
    const flipped = app.extractAssistantChipEvents(
      [{ type: "assistant", message: { content: other } }],
      { flow: FLOW, stepId: "s1", ordinal: 9,
        toolUseIds: ["tu_mix2"], bodyMask: "u" });
    assert.equal(typeof flipped[0]._input, "string",
      "the stripped input was left as a stub object");
    // A mask that names both still covers both.
    const both = app.extractAssistantChipEvents(
      [{ type: "assistant", message: { content: blocks } }],
      { flow: FLOW, stepId: "s1", ordinal: 9,
        toolUseIds: ["tu_mix"], bodyMask: "b" });
    assert.equal(typeof both[0]._input.spec, "string");
  });

  // (L23) --------------------------------------------------------------------
  check("(L23) a retry rewrite moves the record even when it LOOKS identical",
    () => {
      // `stepId#ordinal` is stable across a retry's in-place rewrite by design,
      // and a rewrite whose change sits PAST the kept 96-char head leaves the
      // header, the timestamp and the visible content byte-identical. Only the
      // server's `detail_version` says the body behind that address moved, so a
      // reconcile that ignores it reports the frame as an unchanged duplicate,
      // keeps the old record, and answers the next expand out of the cache with
      // the previous attempt's body.
      const held = progressRecord({ ordinal: 1, step_id: "s1", message: {
        content: "[Bash ✓ one]", tool_use_id: "codex_tool_1", is_error: false,
        tool_detail_lazy: true, detail_flow: FLOW, detail_version: "v1",
      } });
      const rewritten = progressRecord({ ordinal: 1, step_id: "s1", message: {
        content: "[Bash ✓ one]", tool_use_id: "codex_tool_1", is_error: false,
        tool_detail_lazy: true, detail_flow: FLOW, detail_version: "v2",
      } });
      const rec = app.reconcileAppendRecords([held], [rewritten]);
      assert.equal(rec.changed, true,
        "a version-only rewrite was dropped as an unchanged duplicate");
      assert.equal(rec.updatedInPlace, true,
        "the rewrite must invalidate what was cached against the old version");
      assert.equal(
        app.normalizeRecord(rec.records[0]).detailVersion, "v2",
        "the held record kept the previous attempt's detail version");
      // ...and the same delivery twice is still a no-op.
      assert.equal(
        app.reconcileAppendRecords([held], [held]).changed, false,
        "an identical re-delivery must stay a cheap no-op");
    });

  // (L24) --------------------------------------------------------------------
  check("(L24) a summarized re-delivery never displaces a body held inline",
    () => {
      // The ordinary REST∩WS overlap: a live tail append placed the line WHOLE
      // (requirement 7 — it is never lazified), then the idle poll's delta
      // re-carries that same line summarized. The summary knows nothing the
      // held copy lacks, so adopting it would trade an inline body for an
      // on-demand fetch and repaint the pane on every poll.
      const whole = progressRecord({ ordinal: 1, step_id: "s1", message: {
        content: "[Bash ✓ one]", tool_use_id: "codex_tool_1", is_error: false,
        tool_detail: { kind: "text", text: BIG },
      } });
      const summarized = progressRecord({ ordinal: 1, step_id: "s1", message: {
        content: "[Bash ✓ one]", tool_use_id: "codex_tool_1", is_error: false,
        tool_detail_lazy: true, detail_flow: FLOW, detail_version: "v1",
      } });
      const rec = app.reconcileAppendRecords([whole], [summarized]);
      assert.equal(rec.changed, false,
        "a summarized re-delivery displaced an inline body");
      // The reverse IS adopted: the whole copy carries detail the summary
      // cannot answer with, and it retires the version cached before it.
      const back = app.reconcileAppendRecords([summarized], [whole]);
      assert.equal(back.changed, true,
        "a whole re-delivery of a summarized line was dropped");
      assert.equal(back.updatedInPlace, true);
    });

  globalThis.fetch = savedFetch;
}
