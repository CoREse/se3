/*
 * Headless renderer assertion for REAL daemon conversation records.
 *
 * Usage:   node render_real_records.mjs <records.json>
 *
 * <records.json> is the exact `records` array returned by the central
 * server's `GET /api/history/{flow_id}` — i.e. the daemon's
 * `{step_id, step_type, message}` envelopes, where `step_type` was injected
 * by the real daemon from the jsonl file-name convention (NOT faked into the
 * inner `message`). This script feeds those records through the production
 * `app.js` conversation renderer (`renderConversation`) against a minimal
 * DOM stub and asserts the message-rendering paradigm actually takes effect on
 * real data:
 *
 *   - step-section headers read the paradigm names (DISCOVERY / IMPLEMENT /
 *     VERSION ANALYZE …) — never the raw `NN_<type>_<hash>` file stem;
 *   - a discovery assistant turn renders its structured fields (content /
 *     refined_description / questions) rather than dumping the raw ```json```
 *     blob.
 *
 * On success it prints a JSON line `{"ok": true, ...}` and exits 0; on any
 * assertion failure it prints `{"ok": false, "error": ...}` and exits 1. The
 * pytest e2e harness shells out to this and parses the result, so the
 * rendering paradigm is verified on real records without needing a browser.
 *
 * The DOM stub mirrors the one in `test_app_pure.mjs`: only the small slice of
 * the DOM API the conversation renderer touches is implemented.
 */
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import fs from "node:fs";
import path from "node:path";

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const appPath = path.join(here, "..", "..", "src", "se3", "server", "static", "app.js");

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
    if (node && node.tagName === "#FRAGMENT") {
      for (const c of node.childNodes.slice()) this.insertBefore(c, ref);
      node.childNodes = [];
      return node;
    }
    if (node.parentNode) node.parentNode._detach(node);
    node.parentNode = this;
    if (ref == null) { this.childNodes.push(node); return node; }
    const idx = this.childNodes.indexOf(ref);
    if (idx < 0) this.childNodes.push(node);
    else this.childNodes.splice(idx, 0, node);
    return node;
  }
  removeChild(node) { this._detach(node); return node; }
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  closest() { return null; }
  focus() {}
  scrollIntoView() {}
}
function makeText(text) {
  const n = new FakeNode("#text");
  n._text = String(text == null ? "" : text);
  return n;
}

const _byId = {};
globalThis.Node = FakeNode;
globalThis.requestAnimationFrame = () => {};
globalThis.document = {
  createElement: (tag) => new FakeNode(tag),
  createTextNode: (t) => makeText(t),
  createDocumentFragment: () => new FakeNode("#fragment"),
  getElementById: (id) => (_byId[id] = _byId[id] || new FakeNode("div")),
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

function fail(msg) {
  process.stdout.write(JSON.stringify({ ok: false, error: msg }) + "\n");
  process.exit(1);
}

function main() {
  const file = process.argv[2];
  if (!file) fail("usage: render_real_records.mjs <records.json>");
  const records = JSON.parse(fs.readFileSync(file, "utf-8"));
  if (!Array.isArray(records) || records.length === 0) fail("no records to render");

  const app = require(appPath);

  // Guard: the records must be in the REAL daemon shape — authoritative
  // step_type at the envelope, NOT inside the inner message. If a record
  // leaks an inner message.step_type, the test fixture is faking the shape
  // (the exact bug this group fixes) and the assertion is meaningless.
  for (const r of records) {
    if (r && r.message && Object.prototype.hasOwnProperty.call(r.message, "step_type")) {
      fail("record.message leaked a step_type — not a real daemon envelope shape");
    }
  }

  const container = document.createElement("div");
  app.renderConversation(container, records, false);

  // 1. Step-section headers use the paradigm names, never the file stem.
  const titles = findAll(container, "history-step-title").map((h) => h.textContent);
  const stems = records
    .map((r) => String(r.step_id || ""))
    .filter((s) => /^\d+_/.test(s));
  for (const t of titles) {
    for (const stem of stems) {
      if (t === stem) fail(`step header showed the raw file stem '${stem}' instead of a paradigm name`);
    }
  }
  const envTypes = new Set(records.map((r) => String(r.step_type || "").toLowerCase()));
  const expectHeaders = [];
  if (envTypes.has("discovery")) expectHeaders.push("DISCOVERY");
  if (envTypes.has("implement")) expectHeaders.push("IMPLEMENT");
  if (envTypes.has("version_analyze")) expectHeaders.push("VERSION ANALYZE");
  for (const want of expectHeaders) {
    if (!titles.includes(want)) {
      fail(`expected a '${want}' step header; got headers ${JSON.stringify(titles)}`);
    }
  }

  // 2. The discovery assistant turn renders structured fields, not a raw blob.
  //    Find the discovery assistant record, locate its rendered bubble, and
  //    assert the refined_description / content text is present while the raw
  //    JSON key literal is NOT dumped as the primary surface.
  const discAsst = records.find(
    (r) => String(r.step_type).toLowerCase() === "discovery" &&
      r.message && (r.message.role === "assistant"),
  );
  let structured = false;
  if (discAsst) {
    let parsed = null;
    try {
      const body = String(discAsst.message.content || "");
      const m = body.match(/\{[\s\S]*\}/);
      parsed = m ? JSON.parse(m[0]) : null;
    } catch (_e) { parsed = null; }
    if (parsed && (parsed.refined_description || parsed.content)) {
      const wholeText = container.textContent;
      const needle = String(parsed.refined_description || parsed.content);
      if (!wholeText.includes(needle)) {
        fail("discovery refined_description/content was not rendered into the bubble");
      }
      // A structured render must NOT surface the JSON key literal as text.
      if (wholeText.includes('"refined_description":')) {
        fail("discovery turn dumped raw JSON (found '\"refined_description\":' literal) instead of structured fields");
      }
      structured = true;
    }
  }

  process.stdout.write(
    JSON.stringify({
      ok: true,
      headers: titles,
      expected_headers: expectHeaders,
      discovery_structured: structured,
      record_count: records.length,
    }) + "\n",
  );
  process.exit(0);
}

try {
  main();
} catch (e) {
  fail("render crashed: " + (e && e.stack ? e.stack : String(e)));
}
