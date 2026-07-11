/*
 * DOM-free + DOM-stub tests for the WebUI i18n subsystem in app.js (Group G6).
 *
 * Covers the pure language-resolution and key-lookup logic (resolveInitialLang,
 * I18N.lookup / I18N.t with per-key en-US fallback and {param} interpolation),
 * the per-node attribute application (applyNodeTranslations / applyStaticTranslations
 * over data-i18n / data-i18n-placeholder / data-i18n-title), the fetch-failure
 * degradation of I18N.load, and a static-source guard that the shipped locale
 * JSON files exist, are valid, and that zh-CN carries no key absent from the
 * en-US baseline (which holds the key全集).
 *
 * Run manually:  node tests/frontend/i18n_render_switch.test.mjs
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const STATIC_DIR = path.join(here, "..", "..", "src", "se3", "server", "static");
const app = require(path.join(STATIC_DIR, "app.js"));
const { I18N } = app;

let passed = 0;
function check(name, fn) {
  fn();
  passed += 1;
  console.log("  ok -", name);
}
function checkAsync(name, fn) {
  return fn().then(() => {
    passed += 1;
    console.log("  ok -", name);
  });
}

// ---------------------------------------------------------------------------
// resolveInitialLang — localStorage > navigator (exact) > navigator (prefix)
//                       > en-US
// ---------------------------------------------------------------------------
const SUP = ["en-US", "zh-CN"];

check("resolveInitialLang honors a stored exact match first", () => {
  assert.equal(I18N.resolveInitialLang("zh-CN", "en-US", SUP), "zh-CN");
  assert.equal(I18N.resolveInitialLang("en-US", "zh-CN", SUP), "en-US");
});
check("resolveInitialLang ignores an unsupported stored value", () => {
  // A stale / unsupported stored code must not win — fall through to navigator.
  assert.equal(I18N.resolveInitialLang("fr-FR", "zh-CN", SUP), "zh-CN");
});
check("resolveInitialLang falls back to navigator.language exact match", () => {
  assert.equal(I18N.resolveInitialLang(null, "zh-CN", SUP), "zh-CN");
  assert.equal(I18N.resolveInitialLang("", "en-US", SUP), "en-US");
});
check("resolveInitialLang prefix-matches the navigator primary subtag", () => {
  // "zh", "zh-TW", "zh-Hans-CN" all share the primary subtag "zh" → zh-CN.
  assert.equal(I18N.resolveInitialLang(null, "zh", SUP), "zh-CN");
  assert.equal(I18N.resolveInitialLang(null, "zh-TW", SUP), "zh-CN");
  assert.equal(I18N.resolveInitialLang(null, "ZH-hans-cn", SUP), "zh-CN");
});
check("resolveInitialLang falls back to en-US for an unknown navigator lang", () => {
  assert.equal(I18N.resolveInitialLang(null, "fr-FR", SUP), "en-US");
  assert.equal(I18N.resolveInitialLang(null, "", SUP), "en-US");
  assert.equal(I18N.resolveInitialLang(undefined, undefined, SUP), "en-US");
});
check("resolveInitialLang degrades to the first supported when en-US is absent", () => {
  assert.equal(I18N.resolveInitialLang(null, "xx", ["zh-CN", "ja-JP"]), "zh-CN");
  // Empty / bogus supported list → en-US.
  assert.equal(I18N.resolveInitialLang(null, "zh", []), "en-US");
});

// ---------------------------------------------------------------------------
// I18N.lookup / I18N.t — per-key fallback chain + interpolation
// ---------------------------------------------------------------------------
check("lookup returns the active-dict value when present", () => {
  const primary = { "a.b": "primary" };
  const fallback = { "a.b": "fallback" };
  assert.equal(I18N.lookup("a.b", null, primary, fallback), "primary");
});
check("lookup falls back to the baseline dict for a missing key", () => {
  const primary = { "only.primary": "x" };
  const fallback = { "a.b": "fallback" };
  assert.equal(I18N.lookup("a.b", null, primary, fallback), "fallback");
});
check("lookup returns the key itself when neither dict has it", () => {
  assert.equal(I18N.lookup("no.such.key", null, {}, {}), "no.such.key");
});
check("lookup interpolates {name} placeholders from params", () => {
  const d = { greet: "Hi {who}, {n} items" };
  assert.equal(I18N.lookup("greet", { who: "Zed", n: 3 }, d, {}), "Hi Zed, 3 items");
});
check("lookup leaves unmatched placeholders intact and never throws", () => {
  const d = { greet: "Hi {who} and {missing}" };
  assert.equal(I18N.lookup("greet", { who: "Zed" }, d, {}), "Hi Zed and {missing}");
  // params absent → template returned verbatim (no interpolation), no throw.
  assert.equal(I18N.lookup("greet", null, d, {}), "Hi {who} and {missing}");
});

check("I18N.t resolves against the active language with en-US fallback", () => {
  const savedLang = I18N.lang;
  const savedDicts = I18N.dicts;
  try {
    I18N.dicts = {
      "en-US": { "x.only_en": "EN only", "x.both": "EN both" },
      "zh-CN": { "x.both": "ZH both" },
    };
    I18N.lang = "zh-CN";
    // Present in zh-CN → zh value.
    assert.equal(I18N.t("x.both"), "ZH both");
    // Absent in zh-CN, present in en-US → en fallback.
    assert.equal(I18N.t("x.only_en"), "EN only");
    // Absent everywhere → the key itself.
    assert.equal(I18N.t("x.nope"), "x.nope");
  } finally {
    I18N.lang = savedLang;
    I18N.dicts = savedDicts;
  }
});

// ---------------------------------------------------------------------------
// applyNodeTranslations — per-node attribute application
// ---------------------------------------------------------------------------
function fakeNode(attrs) {
  return {
    _attrs: Object.assign({}, attrs),
    textContent: "",
    placeholder: "",
    title: "",
    getAttribute(k) {
      return Object.prototype.hasOwnProperty.call(this._attrs, k)
        ? this._attrs[k] : null;
    },
    setAttribute(k, v) { this._attrs[k] = v; },
  };
}
const idTfn = (k) => `T(${k})`;

check("applyNodeTranslations sets textContent from data-i18n", () => {
  const n = fakeNode({ "data-i18n": "nav.history" });
  n.textContent = "History";
  app.applyNodeTranslations(n, idTfn);
  assert.equal(n.textContent, "T(nav.history)");
});
check("applyNodeTranslations sets placeholder from data-i18n-placeholder", () => {
  const n = fakeNode({ "data-i18n-placeholder": "flow.replyPlaceholder" });
  app.applyNodeTranslations(n, idTfn);
  assert.equal(n.placeholder, "T(flow.replyPlaceholder)");
  assert.equal(n.getAttribute("placeholder"), "T(flow.replyPlaceholder)");
});
check("applyNodeTranslations sets title from data-i18n-title", () => {
  const n = fakeNode({ "data-i18n-title": "flow.usageTitle" });
  app.applyNodeTranslations(n, idTfn);
  assert.equal(n.title, "T(flow.usageTitle)");
  assert.equal(n.getAttribute("title"), "T(flow.usageTitle)");
});
check("applyNodeTranslations applies several attributes on one node", () => {
  const n = fakeNode({
    "data-i18n": "a", "data-i18n-placeholder": "b", "data-i18n-title": "c",
  });
  app.applyNodeTranslations(n, idTfn);
  assert.equal(n.textContent, "T(a)");
  assert.equal(n.placeholder, "T(b)");
  assert.equal(n.title, "T(c)");
});
check("applyNodeTranslations is a no-op on a node with no i18n attrs", () => {
  const n = fakeNode({ id: "x" });
  n.textContent = "untouched";
  app.applyNodeTranslations(n, idTfn);
  assert.equal(n.textContent, "untouched");
});
check("applyNodeTranslations tolerates a node without getAttribute", () => {
  // Must not throw on a text node / bare object.
  app.applyNodeTranslations({}, idTfn);
  app.applyNodeTranslations(null, idTfn);
});

// ---------------------------------------------------------------------------
// I18N.applyStaticTranslations — scans a scope via querySelectorAll
// ---------------------------------------------------------------------------
check("applyStaticTranslations translates every tagged node in a scope", () => {
  const nodes = [
    fakeNode({ "data-i18n": "nav.history" }),
    fakeNode({ "data-i18n-placeholder": "flow.replyPlaceholder" }),
    fakeNode({ "data-i18n-title": "flow.usageTitle" }),
    fakeNode({ id: "plain" }),
  ];
  const scope = {
    querySelectorAll(sel) {
      const attr = sel.slice(1, -1); // "[data-i18n]" → "data-i18n"
      return nodes.filter((n) => n.getAttribute(attr) != null);
    },
  };
  const savedLang = I18N.lang;
  const savedDicts = I18N.dicts;
  try {
    I18N.dicts = {
      "en-US": {
        "nav.history": "History",
        "flow.replyPlaceholder": "No pending items…",
        "flow.usageTitle": "Usage",
      },
      "zh-CN": {
        "nav.history": "历史记录",
        "flow.replyPlaceholder": "暂无待处理项…",
        "flow.usageTitle": "用量",
      },
    };
    I18N.lang = "zh-CN";
    I18N.applyStaticTranslations(scope);
    assert.equal(nodes[0].textContent, "历史记录");
    assert.equal(nodes[1].placeholder, "暂无待处理项…");
    assert.equal(nodes[2].title, "用量");
    assert.equal(nodes[3].textContent, ""); // untagged untouched
  } finally {
    I18N.lang = savedLang;
    I18N.dicts = savedDicts;
  }
});
check("applyStaticTranslations preserves in-markup text when the key is missing", () => {
  // The fetch-failure resilience contract: with empty dictionaries (a boot-time
  // load failure), a tagged node keeps its in-markup English fallback rather
  // than being overwritten with the raw dotted key.
  const node = fakeNode({ "data-i18n": "nav.history" });
  node.textContent = "History"; // the index.html English fallback
  const ph = fakeNode({ "data-i18n-placeholder": "flow.replyPlaceholder" });
  ph.placeholder = "No pending items…";
  const scope = {
    querySelectorAll(sel) {
      const attr = sel.slice(1, -1);
      return [node, ph].filter((n) => n.getAttribute(attr) != null);
    },
  };
  const savedDicts = I18N.dicts;
  const savedLang = I18N.lang;
  try {
    I18N.dicts = { "en-US": {}, "zh-CN": {} }; // total miss
    I18N.lang = "zh-CN";
    I18N.applyStaticTranslations(scope);
    assert.equal(node.textContent, "History", "missing key must keep the fallback text");
    assert.equal(ph.placeholder, "No pending items…", "missing key must keep the fallback placeholder");
  } finally {
    I18N.dicts = savedDicts;
    I18N.lang = savedLang;
  }
});
check("applyStaticTranslations no-ops when the scope has no querySelectorAll", () => {
  // Must not throw in the require-loaded (document-less) environment.
  I18N.applyStaticTranslations({});
  I18N.applyStaticTranslations(null);
});

// ---------------------------------------------------------------------------
// I18N.load — fetch-failure degradation (page stays on the in-markup English)
// ---------------------------------------------------------------------------
await checkAsync("I18N.load degrades a failed fetch to an empty dict (no throw)", async () => {
  const savedFetch = globalThis.fetch;
  const savedDicts = I18N.dicts;
  try {
    globalThis.fetch = async () => { throw new Error("network down"); };
    I18N.dicts = { "en-US": {}, "zh-CN": {} };
    const dict = await I18N.load("zh-CN");
    assert.deepEqual(dict, {}, "a failed fetch resolves to an empty dict");
    // t() then falls back to the key itself — the UI keeps its in-markup text.
    I18N.lang = "zh-CN";
    assert.equal(I18N.t("nav.history"), "nav.history");
  } finally {
    globalThis.fetch = savedFetch;
    I18N.dicts = savedDicts;
    I18N.lang = "en-US";
  }
});
await checkAsync("I18N.load caches a non-empty dict and skips re-fetching", async () => {
  const savedFetch = globalThis.fetch;
  const savedDicts = I18N.dicts;
  try {
    let calls = 0;
    globalThis.fetch = async () => {
      calls += 1;
      return { ok: true, json: async () => ({ "k": "v" }) };
    };
    I18N.dicts = { "en-US": {}, "zh-CN": {} };
    const d1 = await I18N.load("zh-CN");
    assert.equal(d1.k, "v");
    assert.equal(calls, 1);
    // Second load hits the cache — no extra fetch.
    await I18N.load("zh-CN");
    assert.equal(calls, 1, "a cached non-empty dict must not re-fetch");
  } finally {
    globalThis.fetch = savedFetch;
    I18N.dicts = savedDicts;
    I18N.lang = "en-US";
  }
});

// ---------------------------------------------------------------------------
// Shipped locale JSON files — valid, and zh-CN ⊆ en-US key set (baseline holds
// the key全集; every zh-CN key must exist in en-US so the fallback chain works)
// ---------------------------------------------------------------------------
check("locale JSON files exist, parse, and share the SUPPORTED codes", () => {
  for (const code of I18N.SUPPORTED) {
    const p = path.join(STATIC_DIR, "i18n", `${code}.json`);
    assert.ok(fs.existsSync(p), `missing locale file ${code}.json`);
    const dict = JSON.parse(fs.readFileSync(p, "utf8"));
    assert.ok(Object.keys(dict).length > 0, `${code}.json is empty`);
  }
});
check("en-US is the baseline: every zh-CN key exists in en-US", () => {
  const en = JSON.parse(
    fs.readFileSync(path.join(STATIC_DIR, "i18n", "en-US.json"), "utf8"));
  const zh = JSON.parse(
    fs.readFileSync(path.join(STATIC_DIR, "i18n", "zh-CN.json"), "utf8"));
  const missing = Object.keys(zh).filter((k) => !(k in en));
  assert.deepEqual(missing, [],
    `zh-CN has keys absent from the en-US baseline: ${missing.join(", ")}`);
  // The endonym labels must be identical across dictionaries (native names).
  for (const code of I18N.SUPPORTED) {
    assert.equal(en[`lang.${code}`], zh[`lang.${code}`],
      `endonym lang.${code} must match across dictionaries`);
  }
});

console.log(`\n${passed} i18n check(s) passed.`);
