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
 * real data.
 *
 * The paradigm judgement itself lives in `render_in_browser.mjs`
 * (`paradigmAssertions`) — the SINGLE source shared verbatim with the real
 * Chromium acceptance test, so the node-stub and in-browser checks can never
 * drift. This script only owns the node-side plumbing: the DOM stub, loading
 * `app.js` via `require`, and printing the result for the pytest harness.
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
// Side-effect import: populates `globalThis.__se3Paradigm` with the shared
// `paradigmAssertions` judgement (the same source the browser test injects).
import "./render_in_browser.mjs";

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const appPath = path.join(here, "..", "..", "src", "tianluo", "server", "static", "app.js");

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
  const container = document.createElement("div");
  app.renderConversation(container, records, false);

  // The paradigm judgement is the shared single source — the same
  // `paradigmAssertions` the real-browser acceptance test runs in-page — so the
  // node-stub and in-browser DOM checks can never drift apart.
  const paradigm = globalThis.__se3Paradigm;
  if (!paradigm || typeof paradigm.paradigmAssertions !== "function") {
    fail("shared paradigm assertions (render_in_browser.mjs) failed to load");
  }
  const result = paradigm.paradigmAssertions(records, container);
  if (!result.ok) fail(result.error || "paradigm assertion failed");

  process.stdout.write(
    JSON.stringify({
      ok: true,
      headers: result.headers,
      expected_headers: result.expected_headers,
      discovery_structured: result.discovery_structured,
      discovery_proposed_card: result.discovery_proposed_card,
      user_literal_only: result.user_literal_only,
      raw_nested: result.raw_nested,
      report_card_present: result.report_card_present,
      record_count: result.record_count,
    }) + "\n",
  );
  process.exit(0);
}

try {
  main();
} catch (e) {
  fail("render crashed: " + (e && e.stack ? e.stack : String(e)));
}
