/*
 * ADJUDICATE approval-review tests (Group G4).
 *
 * When a CONFIRM gate reviews an ADJUDICATE ruling, the web console cannot let
 * the operator decide 批 vs 打回 blind: `renderAdjudicateReview(target)` surfaces
 * the ruling's `adjudication_rationale` panel plus a baseline→adjudicated_description
 * before/after diff (reusing the shared unified-diff renderer). The backend
 * (build_adjudicate_review_context) injects those three fields into
 * `target.context`; every field is best-effort, so the block degrades gracefully
 * when any subset is missing and returns null for a non-adjudicate target.
 *
 * This file is dual-mode:
 *   - `registerAdjudicateReviewTests(ctx)` is imported by tests/frontend/
 *     test_app_pure.mjs so the checks run under that harness's shared DOM stub
 *     (and thus inside the pytest bridge that shells out to the Node runner).
 *   - Run directly (`node tests/frontend/adjudicate_review.test.mjs`) it installs
 *     its own DOM stub and runs the same checks, so the acceptance criterion
 *     "node tests/frontend/adjudicate_review.test.mjs 通过" holds without the
 *     parent harness.
 */
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

// ---------------------------------------------------------------------------
// The registrable test body — shared between the harness and standalone paths.
// ---------------------------------------------------------------------------
export async function registerAdjudicateReviewTests(ctx) {
  const { app, check, findOne, findAll } = ctx;

  // Build a bare confirm target whose context carries the given adjudicate
  // fields. `renderAdjudicateReview` only reads `target.context`, so this is all
  // the shape it needs — no flow / state wiring required for the pure checks.
  function target({ type = "adjudicate", rationale, baseline, description } = {}) {
    const context = { flow_id: "flow-x", step_to_review_type: type };
    if (rationale !== undefined) context.adjudication_rationale = rationale;
    if (baseline !== undefined) context.baseline = baseline;
    if (description !== undefined) context.adjudicated_description = description;
    return { kind: "confirm", callId: "cfADJ", context };
  }

  // Concatenate every `.diff-content` cell whose row carries `cls` — used to
  // assert an add / del line contains the expected text regardless of the "+"/"-"
  // prefix the renderer keeps on the content span.
  function diffTextFor(root, cls) {
    return findAll(root, cls)
      .map((row) => {
        const content = findOne(row, "diff-content");
        return content ? content.textContent : "";
      })
      .join("\n");
  }

  // ---- rationale panel + before/after diff render --------------------------
  check("G4 adjudicate review renders the rationale panel and a baseline→description diff", () => {
    const node = app.renderAdjudicateReview(
      target({
        rationale: "两轮在实现语言上震荡,裁定采用 Rust 收敛。",
        baseline: "原始任务描述\n用 Python 实现 X",
        description: "原始任务描述\n用 Rust 实现 X(裁决修正)",
      }),
    );
    assert.ok(node, "an adjudicate target renders a review block");
    assert.ok(node.classList.contains("flow-reply-adjudicate"), "review is wrapped in .flow-reply-adjudicate");

    const body = findOne(node, "flow-reply-adjudicate-rationale-body");
    assert.ok(body, "rationale panel rendered");
    assert.equal(body.textContent, "两轮在实现语言上震荡,裁定采用 Rust 收敛。");

    // The diff must reflect baseline→description: the Python line deleted, the
    // Rust line added. Reuses the shared .diff-add/.diff-del renderer.
    const added = diffTextFor(node, "diff-add");
    const deleted = diffTextFor(node, "diff-del");
    assert.ok(added.includes("用 Rust 实现 X(裁决修正)"), `added lines show the new description, got: ${added}`);
    assert.ok(deleted.includes("用 Python 实现 X"), `deleted lines show the old baseline, got: ${deleted}`);
    // The unchanged first line must NOT appear as an add or a del.
    assert.ok(!added.includes("原始任务描述"), "unchanged line is not an addition");
    assert.ok(!deleted.includes("原始任务描述"), "unchanged line is not a deletion");
  });

  // ---- non-adjudicate target renders nothing -------------------------------
  check("G4 a non-adjudicate confirm target renders no review block", () => {
    assert.equal(app.renderAdjudicateReview(target({ type: "plan", rationale: "x" })), null,
      "a plan confirm must not render the adjudicate review");
    assert.equal(app.renderAdjudicateReview({ kind: "confirm", context: null }), null,
      "a context-less target must not render the adjudicate review");
    assert.equal(app.renderAdjudicateReview(null), null,
      "a null target must not throw and renders nothing");
  });

  // ---- graceful degradation: missing baseline ------------------------------
  check("G4 a missing baseline degrades to an all-added diff without throwing", () => {
    let node;
    assert.doesNotThrow(() => {
      node = app.renderAdjudicateReview(
        target({ rationale: "首次裁决,无既有基线。", description: "全新任务描述\n实现 Y" }),
      );
    }, "a missing baseline must not throw");
    assert.ok(node, "the review still renders with only the available fields");
    assert.equal(findOne(node, "flow-reply-adjudicate-rationale-body").textContent, "首次裁决,无既有基线。");
    // With no baseline, the new description shows entirely as additions.
    const added = diffTextFor(node, "diff-add");
    assert.ok(added.includes("全新任务描述") && added.includes("实现 Y"),
      `the whole new description is added when baseline is absent, got: ${added}`);
  });

  // ---- graceful degradation: ruling changed no description -----------------
  check("G4 a ruling that changed no description shows a note, not a delete-all diff", () => {
    const node = app.renderAdjudicateReview(
      target({ rationale: "维持原描述,仅澄清计划。", baseline: "既有任务描述\n实现 X", description: "" }),
    );
    assert.ok(node, "a description-only-null ruling still renders (rationale present)");
    // No delete-all diff — the baseline must NOT be rendered as deleted lines.
    assert.equal(findAll(node, "diff-del").length, 0, "no delete-all diff for an unchanged description");
    assert.equal(findAll(node, "diff-add").length, 0, "no add lines for an unchanged description");
    const note = findOne(node, "flow-reply-adjudicate-note");
    assert.ok(note, "an explanatory note is shown instead of a diff");
    assert.ok(String(note.textContent).includes("did not modify"), `note explains nothing changed, got: ${note.textContent}`);
  });

  // ---- fully empty context renders nothing ---------------------------------
  check("G4 an adjudicate target with no rationale/baseline/description renders nothing", () => {
    assert.equal(app.renderAdjudicateReview(target({})), null,
      "an adjudicate target carrying none of the display fields degrades to null");
  });

  // ---- integration: block appears in the docked reply box for adjudicate ---
  check("G4 updateReplyBox surfaces the adjudicate review only for an adjudicate confirm chip", () => {
    const savedSetTimeout = globalThis.setTimeout;
    globalThis.setTimeout = () => 0;
    try {
      function renderChip(context) {
        app.state.selectedFlowId = "flow-x";
        app.state.flowConversationRecords = [];
        app.state.flowDetail = {
          flow_id: "flow-x",
          status: "running",
          pending_calls: [{ call_id: "cf1", kind: "confirm", prompt: "请审批", context }],
        };
        app.state.pendingSendTimer = null;
        app.state.pendingSendSettleKey = null;
        app.state.pendingSendBaselineCallIds = null;
        app.state.flowReplyPromptExpanded = {};
        app.state.flowReplyPromptScroll = {};
        const entries = app.computeInterventions(app.state.flowDetail);
        app.state.flowInterventions = entries;
        app.state.flowReplyTargetId = entries[0].id;
        app.updateReplyBox(app.state.flowDetail);
        return document.getElementById("flow-reply-context");
      }

      const adjCtx = renderChip({
        flow_id: "flow-x",
        step_to_review_type: "adjudicate",
        adjudication_rationale: "裁定收敛。",
        baseline: "旧描述",
        adjudicated_description: "新描述",
      });
      assert.ok(findOne(adjCtx, "flow-reply-adjudicate"), "adjudicate confirm chip shows the review block");
      // The 批准/打回 buttons still render alongside it.
      assert.ok(findOne(adjCtx, "flow-reply-confirm-approve"), "the decision buttons coexist with the review");

      const planCtx = renderChip({
        flow_id: "flow-x",
        step_to_review_type: "plan",
        adjudication_rationale: "should be ignored",
      });
      assert.equal(findOne(planCtx, "flow-reply-adjudicate"), null,
        "a plan confirm chip does not render the adjudicate review block");
    } finally {
      globalThis.setTimeout = savedSetTimeout;
      app.settlePendingSend();
    }
  });
}

// ---------------------------------------------------------------------------
// Standalone bootstrap: install a self-contained DOM stub and run the checks.
// Only executed when this module is the process entry point; when imported by
// test_app_pure.mjs the export above is used against that harness's stub.
// ---------------------------------------------------------------------------
function isMainEntry() {
  try {
    return process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1]);
  } catch (_) {
    return false;
  }
}

if (isMainEntry()) {
  // -- Minimal-but-sufficient DOM stub (mirrors the FakeNode in
  //    tests/frontend/test_app_pure.mjs / confirm_chip.test.mjs).
  class FakeNode {
    constructor(tag) {
      this.tagName = String(tag || "").toUpperCase();
      this.nodeType = this.tagName === "#TEXT" ? 3 : 1;
      this._classes = new Set();
      this.childNodes = [];
      this._text = "";
      this.dataset = {};
      this.style = {};
      this._listeners = {};
      this.parentNode = null;
      this.type = "";
      this.title = "";
      this.id = "";
      this.value = "";
      this.disabled = false;
      this.placeholder = "";
      this.scrollTop = 0;
      this.scrollHeight = 0;
      this.clientHeight = 0;
      this.classList = {
        add: (...cs) => { for (const c of cs) if (c) this._classes.add(c); },
        remove: (...cs) => { for (const c of cs) this._classes.delete(c); },
        contains: (c) => this._classes.has(c),
        toggle: (c, force) => {
          const want = force === undefined ? !this._classes.has(c) : !!force;
          if (want) this._classes.add(c); else this._classes.delete(c);
          return want;
        },
      };
    }
    set className(v) { this._classes = new Set(String(v || "").split(/\s+/).filter(Boolean)); }
    get className() { return Array.from(this._classes).join(" "); }
    set textContent(v) { this._text = String(v == null ? "" : v); this.childNodes = []; }
    get textContent() {
      if (this.childNodes.length) return this.childNodes.map((c) => c.textContent).join("");
      return this._text;
    }
    set innerHTML(_v) { this.childNodes = []; this._text = ""; }
    get innerHTML() { return ""; }
    get children() { return this.childNodes.filter((c) => c && c.nodeType !== 3); }
    get firstChild() { return this.childNodes[0] || null; }
    get lastChild() { return this.childNodes.length ? this.childNodes[this.childNodes.length - 1] : null; }
    _detach(node) {
      const i = this.childNodes.indexOf(node);
      if (i >= 0) { this.childNodes.splice(i, 1); node.parentNode = null; }
    }
    appendChild(child) {
      if (child && child.tagName === "#FRAGMENT") {
        for (const c of child.childNodes.slice()) this.appendChild(c);
        child.childNodes = [];
        return child;
      }
      if (child.parentNode) child.parentNode._detach(child);
      child.parentNode = this;
      this.childNodes.push(child);
      return child;
    }
    append(...nodes) {
      for (const n of nodes) this.appendChild(typeof n === "string" ? makeText(n) : n);
    }
    insertBefore(node, ref) {
      if (node.parentNode) node.parentNode._detach(node);
      node.parentNode = this;
      if (ref == null) { this.childNodes.push(node); return node; }
      const idx = this.childNodes.indexOf(ref);
      if (idx < 0) this.childNodes.push(node); else this.childNodes.splice(idx, 0, node);
      return node;
    }
    removeChild(node) { this._detach(node); return node; }
    remove() { if (this.parentNode) this.parentNode._detach(this); }
    addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
    dispatch(type) {
      for (const fn of (this._listeners[type] || []).slice()) fn({ preventDefault() {} });
    }
    closest() { return null; }
    focus() {}
    scrollIntoView() {}
    getBoundingClientRect() { return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }; }
    scrollTo(opts) { if (opts && typeof opts.top === "number") this.scrollTop = opts.top; }
    get nextElementSibling() { return null; }
  }
  function makeText(text) {
    const n = new FakeNode("#text");
    n._text = String(text == null ? "" : text);
    return n;
  }
  const _elementsById = {};
  globalThis.Node = FakeNode;
  globalThis.requestAnimationFrame = () => {};
  globalThis.document = {
    createElement: (tag) => new FakeNode(tag),
    createTextNode: (text) => makeText(text),
    createDocumentFragment: () => new FakeNode("#fragment"),
    getElementById: (id) => {
      if (!_elementsById[id]) _elementsById[id] = new FakeNode("div");
      return _elementsById[id];
    },
    addEventListener: () => {},
  };
  function findAll(node, cls, acc = []) {
    if (!node || !node.childNodes) return acc;
    for (const c of node.childNodes) {
      if (c.classList && c.classList.contains(cls)) acc.push(c);
      findAll(c, cls, acc);
    }
    return acc;
  }
  function findOne(node, cls) { return findAll(node, cls)[0] || null; }

  const require = createRequire(import.meta.url);
  const here = path.dirname(fileURLToPath(import.meta.url));
  const app = require(path.join(here, "..", "..", "src", "se3", "server", "static", "app.js"));

  let passed = 0;
  let failed = 0;
  function check(name, fn) {
    try { fn(); passed += 1; console.log("  ok -", name); }
    catch (e) { failed += 1; console.error("  FAIL -", name, "\n", e && e.stack ? e.stack : e); }
  }
  async function checkAsync(name, fn) {
    try { await fn(); passed += 1; console.log("  ok -", name); }
    catch (e) { failed += 1; console.error("  FAIL -", name, "\n", e && e.stack ? e.stack : e); }
  }

  await registerAdjudicateReviewTests({ app, check, checkAsync, findOne, findAll });

  console.log(`\nadjudicate_review: ${passed} passed, ${failed} failed — ${passed} checks passed`);
  if (failed > 0) process.exit(1);
}
