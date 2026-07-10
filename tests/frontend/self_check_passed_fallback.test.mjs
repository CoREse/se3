/*
 * self_check status-bar fallback tests.
 *
 * Root cause of the "green ✓ PASSED above a list of issues" bug: an assistant
 * message rendered inline in the conversation goes through
 * makeStructuredAssistantRenderer, which hands the LLM's raw JSON to
 * renderSelfCheckReport as synthetic `outputs`. That JSON carries only
 * issues / previous_issue_resolutions / summary — `actionable_count` is added
 * later by self_check_handler, after `_validate_and_filter_issues`. The
 * renderer used to fall back to `0` on the missing key, hit its
 * `actionable === 0` branch and paint ✓ PASSED while the issue list below it
 * said otherwise. The fix derives the count from `issues.length` when the key
 * is absent, and words that path neutrally ("N issue(s)") because those raw
 * issues have not been validated as actionable.
 *
 * This file is dual-mode:
 *   - `registerSelfCheckPassedFallbackTests(ctx)` is imported by
 *     tests/frontend/test_app_pure.mjs so the checks run under that harness's
 *     shared DOM stub (and thus inside the pytest bridge that shells out to the
 *     Node runner).
 *   - Run directly (`node tests/frontend/self_check_passed_fallback.test.mjs`)
 *     it installs its own DOM stub and runs the same checks.
 */
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

// ---------------------------------------------------------------------------
// The registrable test body — shared between the harness and standalone paths.
// ---------------------------------------------------------------------------
export async function registerSelfCheckPassedFallbackTests(ctx) {
  const { app, check, findOne } = ctx;

  // Go through the renderer table rather than a private export: this also pins
  // the contract that self_check is registered there at all.
  function render(outputs, status = "completed") {
    return app.STEP_REPORT_RENDERERS.self_check({ status }, outputs);
  }
  function label(frag) {
    const node = findOne(frag, "step-report__label");
    assert.ok(node, "a status-bar label is rendered");
    return node;
  }

  // ---- regression: synthetic assistant outputs have no actionable_count ----
  check("self_check_passed_fallback: missing actionable_count with issues renders a failure label, never ✓ PASSED", () => {
    const frag = render({
      issues: [{ severity: "high", description: "engine.json cache is stat-keyed" }],
    });
    const node = label(frag);
    assert.ok(node.classList.contains("fail"), `label must be a failure label, got class ${node.className}`);
    assert.equal(node.textContent, "✗ 1 issue(s)");
    assert.ok(!frag.textContent.includes("✓ PASSED"),
      `no ✓ PASSED anywhere in the card, got: ${frag.textContent}`);
  });

  // ---- missing actionable_count, no issues → genuinely passed --------------
  check("self_check_passed_fallback: missing actionable_count with an empty issues list renders ✓ PASSED", () => {
    const node = label(render({ issues: [] }));
    assert.ok(node.classList.contains("ok"), `label must be an ok label, got class ${node.className}`);
    assert.equal(node.textContent, "✓ PASSED");
  });

  // ---- real step.outputs path: behavior unchanged --------------------------
  check("self_check_passed_fallback: actionable_count 0 renders ✓ PASSED", () => {
    const node = label(render({ actionable_count: 0, issues: [] }));
    assert.ok(node.classList.contains("ok"), `label must be an ok label, got class ${node.className}`);
    assert.equal(node.textContent, "✓ PASSED");
  });

  check("self_check_passed_fallback: a present actionable_count keeps the actionable wording", () => {
    const node = label(render({
      actionable_count: 3,
      issues: [
        { severity: "high", description: "a" },
        { severity: "medium", description: "b" },
        { severity: "low", description: "c" },
      ],
    }));
    assert.ok(node.classList.contains("fail"), `label must be a failure label, got class ${node.className}`);
    assert.equal(node.textContent, "✗ 3 actionable issue(s)");
  });

  // ---- failed status wins over any outputs-derived branch ------------------
  check("self_check_passed_fallback: status failed renders ✗ FAILED ahead of a would-be ✓ PASSED", () => {
    const node = label(render({ actionable_count: 0, issues: [] }, "failed"));
    assert.ok(node.classList.contains("fail"), `label must be a failure label, got class ${node.className}`);
    assert.equal(node.textContent, "✗ FAILED");
  });

  // ---- robustness: a non-array issues value must not throw -----------------
  check("self_check_passed_fallback: a non-array issues value degrades to ✓ PASSED without throwing", () => {
    let frag;
    assert.doesNotThrow(() => { frag = render({ issues: null }); },
      "a null issues value must not throw");
    const node = label(frag);
    assert.ok(node.classList.contains("ok"), `label must be an ok label, got class ${node.className}`);
    assert.equal(node.textContent, "✓ PASSED");
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
  class FakeNode {
    constructor(tag) {
      this.tagName = String(tag || "").toUpperCase();
      this.nodeType = this.tagName === "#TEXT" ? 3 : 1;
      this._classes = new Set();
      this.childNodes = [];
      this._text = "";
      this.dataset = {};
      this.style = {};
      this.parentNode = null;
      this.classList = {
        add: (...cs) => { for (const c of cs) if (c) this._classes.add(c); },
        remove: (...cs) => { for (const c of cs) this._classes.delete(c); },
        contains: (c) => this._classes.has(c),
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
    removeChild(node) { this._detach(node); return node; }
    addEventListener() {}
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

  await registerSelfCheckPassedFallbackTests({ app, check, findOne, findAll });

  console.log(`\nself_check_passed_fallback: ${passed} passed, ${failed} failed — ${passed} checks passed`);
  if (failed > 0) process.exit(1);
}
