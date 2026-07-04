/*
 * CONFIRM approval-gate chip tests (Group G3).
 *
 * The web console renders a `kind: 'confirm'` pending call (plan 确认 /
 * adjudicate 裁决审批 / per-step review) as an explicit 批准/打回 pair plus an
 * optional note textarea, and POSTs a STRUCTURED {response:{approved,feedback}}
 * decision — so an operator can never silently mis-approve/mis-reject the way
 * the old single free-text box did (typing "1" or "同意" was quietly treated as
 * a打回). The retained free-text box mirrors run.py's approval/rejection token
 * sets: "同意"/"批准" send an approval, "打回"/"拒绝" send a rejection, and any
 * unrecognized note ("1", free prose) is only ever sent as a revision AFTER an
 * explicit "will be treated as a revision request — sure?" second-guess.
 *
 * This file is dual-mode:
 *   - `registerConfirmChipTests(ctx)` is imported by tests/frontend/
 *     test_app_pure.mjs so the checks run under that harness's shared DOM stub
 *     (and thus inside the pytest bridge that shells out to the Node runner).
 *   - Run directly (`node tests/frontend/confirm_chip.test.mjs`) it installs its
 *     own DOM stub and runs the same checks, so the acceptance criterion
 *     "node tests/frontend/confirm_chip.test.mjs 通过" holds without the parent
 *     harness.
 */
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

// ---------------------------------------------------------------------------
// The registrable test body — shared between the harness and standalone paths.
// ---------------------------------------------------------------------------
export async function registerConfirmChipTests(ctx) {
  const { app, check, checkAsync, findOne, findAll } = ctx;

  // Stub the network + scheduling so no request escapes and no 8s settle timer
  // outlives the test. `installFetch` records the last POST so the structured
  // body can be asserted; JSON.parse of the body throws loudly if the send
  // shape regresses away from a JSON string.
  let lastReq = null;
  let fetchCalls = 0;
  const savedFetch = globalThis.fetch;
  const savedSetTimeout = globalThis.setTimeout;
  const savedConfirm = globalThis.confirm;

  function installFetch({ ok = true, status = 200 } = {}) {
    fetchCalls = 0;
    lastReq = null;
    globalThis.fetch = (input, init) => {
      fetchCalls += 1;
      lastReq = {
        input: String(input),
        init: init || {},
        body: init && init.body != null ? JSON.parse(init.body) : null,
      };
      return Promise.resolve({ ok, status, json: () => Promise.resolve({}) });
    };
  }
  function restoreGlobals() {
    globalThis.fetch = savedFetch;
    globalThis.setTimeout = savedSetTimeout;
    globalThis.confirm = savedConfirm;
    app.settlePendingSend();
  }
  async function flush() {
    for (let i = 0; i < 6; i += 1) await Promise.resolve();
  }

  // Seed a running flow whose only pending call is a CONFIRM gate, wire the
  // interventions + reply target, and render the docked reply box.
  function renderConfirmChip({ callId = "cf1", kind = "confirm" } = {}) {
    globalThis.setTimeout = () => 0;
    app.state.selectedFlowId = "flow-x";
    app.state.flowConversationRecords = [];
    app.state.flowDetail = {
      flow_id: "flow-x",
      status: "running",
      pending_calls: [
        {
          call_id: callId,
          kind,
          prompt: "请审批该裁决",
          // context.flow_id must match so pendingCalls's flow-scoping filter
          // keeps the call (an unannotated call is dropped as cross-scenario).
          context: { flow_id: "flow-x", step_to_review_type: "adjudicate" },
        },
      ],
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
    return { target: entries[0], ctx: document.getElementById("flow-reply-context") };
  }

  // ---- render: buttons + note textarea materialize -------------------------
  check("G3 confirm chip renders 批准/打回 buttons and an optional note textarea", () => {
    const { ctx } = renderConfirmChip();
    const approve = findOne(ctx, "flow-reply-confirm-approve");
    const reject = findOne(ctx, "flow-reply-confirm-reject");
    const note = findOne(ctx, "flow-reply-confirm-note");
    assert.ok(approve, "批准 button rendered");
    assert.ok(reject, "打回 button rendered");
    assert.ok(note, "note textarea rendered");
    assert.equal(approve.textContent, "批准");
    assert.equal(reject.textContent, "打回");
    assert.equal(note.tagName, "TEXTAREA");
    // The docked free-text box advertises the recognized approval/rejection
    // words for the fallback path.
    const input = document.getElementById("flow-reply-input");
    assert.ok(
      String(input.placeholder).includes("批准") && String(input.placeholder).includes("修改请求"),
      `confirm placeholder must list usable words, got ${input.placeholder}`,
    );
    restoreGlobals();
  });

  // ---- legacy (kind-less) confirm degrades to plain free-text --------------
  check("G3 legacy confirm chip without a kind degrades to the free-text box (no buttons, no error)", () => {
    // A call file that predates G1 carries no `kind`; normalizeKind folds it to
    // "call", so it must render as an ordinary reply chip — no 批准/打回 buttons
    // — and must not throw.
    const { ctx, target } = renderConfirmChip({ callId: "legacy", kind: null });
    assert.equal(target.kind, "call", "a kind-less call normalizes to a plain call chip");
    assert.equal(findOne(ctx, "flow-reply-confirm"), null, "no confirm decision panel for a legacy chip");
    assert.equal(findOne(ctx, "flow-reply-confirm-approve"), null, "no approve button for a legacy chip");
    restoreGlobals();
  });

  // ---- click 批准 -> structured {approved:true, feedback:null} -------------
  await checkAsync("G3 clicking 批准 POSTs a structured {approved:true, feedback:null}", async () => {
    installFetch({ ok: true });
    const { ctx } = renderConfirmChip({ callId: "cfA" });
    // A blank note normalizes to null so an approval carries no spurious note.
    findOne(ctx, "flow-reply-confirm-approve").dispatch("click");
    await flush();
    assert.equal(fetchCalls, 1, "exactly one POST");
    assert.ok(lastReq.input.includes("/api/flows/flow-x/respond"), "POSTs to the flow's /respond endpoint");
    assert.deepEqual(lastReq.body, {
      response: { approved: true, feedback: null },
      call_id: "cfA",
    });
    restoreGlobals();
  });

  // ---- click 打回 with a note -> {approved:false, feedback:<note>} ---------
  await checkAsync("G3 clicking 打回 with a note POSTs {approved:false, feedback:<note>}", async () => {
    installFetch({ ok: true });
    const { ctx } = renderConfirmChip({ callId: "cfR" });
    findOne(ctx, "flow-reply-confirm-note").value = "  基线方向反了  ";
    findOne(ctx, "flow-reply-confirm-reject").dispatch("click");
    await flush();
    assert.equal(fetchCalls, 1, "exactly one POST");
    assert.deepEqual(lastReq.body, {
      response: { approved: false, feedback: "基线方向反了" },
      call_id: "cfR",
    });
    restoreGlobals();
  });

  // ---- direct sendConfirmDecision body shape (approve carries the note) ----
  await checkAsync("G3 sendConfirmDecision preserves a non-blank approval note", async () => {
    installFetch({ ok: true });
    renderConfirmChip({ callId: "cfN" });
    const target = app.state.flowInterventions[0];
    await app.sendConfirmDecision("flow-x", target, true, "看起来不错");
    await flush();
    assert.deepEqual(lastReq.body, {
      response: { approved: true, feedback: "看起来不错" },
      call_id: "cfN",
    });
    restoreGlobals();
  });

  // ---- free-text mirror: '同意' sends an approval directly -----------------
  await checkAsync("G3 free-text '同意' is recognized as an approval and sent, no dialog", async () => {
    installFetch({ ok: true });
    renderConfirmChip({ callId: "cfY" });
    let dialogs = 0;
    globalThis.confirm = () => { dialogs += 1; return false; };
    document.getElementById("flow-reply-input").value = "同意";
    app.submitReply({ preventDefault() {} });
    await flush();
    assert.equal(dialogs, 0, "an approval word must NOT trigger the revision second-guess");
    assert.equal(fetchCalls, 1, "the approval is sent");
    assert.deepEqual(lastReq.body, {
      response: { approved: true, feedback: null },
      call_id: "cfY",
    });
    restoreGlobals();
  });

  // ---- free-text mirror: '打回' sends a rejection directly -----------------
  await checkAsync("G3 free-text '打回' is recognized as a rejection and sent, no dialog", async () => {
    installFetch({ ok: true });
    renderConfirmChip({ callId: "cfD" });
    let dialogs = 0;
    globalThis.confirm = () => { dialogs += 1; return false; };
    document.getElementById("flow-reply-input").value = "打回";
    app.submitReply({ preventDefault() {} });
    await flush();
    assert.equal(dialogs, 0, "a rejection word must NOT trigger the revision second-guess");
    assert.equal(fetchCalls, 1, "the rejection is sent");
    assert.deepEqual(lastReq.body, {
      response: { approved: false, feedback: "打回" },
      call_id: "cfD",
    });
    restoreGlobals();
  });

  // ---- free-text unknown '1': cancelled second-guess sends nothing ---------
  await checkAsync("G3 free-text '1' triggers the revision second-guess; cancelling sends nothing", async () => {
    installFetch({ ok: true });
    renderConfirmChip({ callId: "cf1x" });
    let dialogs = 0;
    globalThis.confirm = () => { dialogs += 1; return false; }; // operator cancels
    document.getElementById("flow-reply-input").value = "1";
    app.submitReply({ preventDefault() {} });
    await flush();
    assert.equal(dialogs, 1, "an unrecognized '1' must ask before treating it as a revision");
    assert.equal(fetchCalls, 0, "cancelling the second-guess sends NOTHING (no silent 打回)");
    restoreGlobals();
  });

  // ---- free-text unknown '1': confirmed second-guess sends a revision ------
  await checkAsync("G3 free-text '1' confirmed sends a revision {approved:false, feedback:'1'}", async () => {
    installFetch({ ok: true });
    renderConfirmChip({ callId: "cf1y" });
    let dialogs = 0;
    globalThis.confirm = () => { dialogs += 1; return true; }; // operator confirms
    document.getElementById("flow-reply-input").value = "1";
    app.submitReply({ preventDefault() {} });
    await flush();
    assert.equal(dialogs, 1, "the second-guess is shown once");
    assert.equal(fetchCalls, 1, "confirming sends the revision");
    assert.deepEqual(lastReq.body, {
      response: { approved: false, feedback: "1" },
      call_id: "cf1y",
    });
    restoreGlobals();
  });

  // ---- pure token mirror stays in lockstep with run.py ---------------------
  check("G3 interpretConfirmAnswer mirrors run.py: EN + ZH approve/reject, unknown falls through", () => {
    for (const w of ["approve", "yes", "ok", "同意", "通过", "批准", "确认"]) {
      assert.equal(app.interpretConfirmAnswer(w), "approve", `${w} → approve`);
    }
    for (const w of ["no", "reject", "revise", "驳回", "拒绝", "打回", "否决"]) {
      assert.equal(app.interpretConfirmAnswer(w), "reject", `${w} → reject`);
    }
    // "request changes" only matches as a whole string (first-word is "request").
    assert.equal(app.interpretConfirmAnswer("request changes"), "reject");
    // '1' and free prose are unknown → the caller second-guesses them.
    for (const w of ["1", "", "让我再想想", "maybe later"]) {
      assert.equal(app.interpretConfirmAnswer(w), "unknown", `${w} → unknown`);
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
  //    tests/frontend/test_app_pure.mjs) so app.js's DOM builders run headless.
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

  await registerConfirmChipTests({ app, check, checkAsync, findOne, findAll });

  console.log(`\nconfirm_chip: ${passed} passed, ${failed} failed — ${passed} checks passed`);
  if (failed > 0) process.exit(1);
}
