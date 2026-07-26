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
const STATIC_DIR = path.join(here, "..", "..", "src", "tianluo", "server", "static");
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
// resolveInitialLang — localStorage > navigator > en-US. The stored value is
//                       matched by case-insensitive EXACT compare only (no
//                       primary-subtag guessing, no fallthrough to navigator);
//                       navigator.language additionally prefix-matches.
// ---------------------------------------------------------------------------
const SUP = ["en-US", "zh-CN"];

check("resolveInitialLang honors a stored exact match first", () => {
  assert.equal(I18N.resolveInitialLang("zh-CN", "en-US", SUP), "zh-CN");
  assert.equal(I18N.resolveInitialLang("en-US", "zh-CN", SUP), "en-US");
  // Exact, but case-insensitively so — a hand-edited "ZH-cn" still resolves.
  assert.equal(I18N.resolveInitialLang("ZH-cn", "en-US", SUP), "zh-CN");
});
check("resolveInitialLang sends an unsupported stored value to en-US", () => {
  // A stored code is an explicit user choice: when it names no supported
  // language it resolves to en-US, and the browser locale never gets a say.
  assert.equal(I18N.resolveInitialLang("fr-FR", "zh-CN", SUP), "en-US");
  // No primary-subtag guessing on the stored value: "zh" / "zh-TW" name no
  // supported language, so they land on the baseline rather than being
  // silently re-pointed at zh-CN. Prefix matching is the navigator layer only.
  assert.equal(I18N.resolveInitialLang("zh", "en-US", SUP), "en-US");
  assert.equal(I18N.resolveInitialLang("zh-TW", "zh-CN", SUP), "en-US");
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
check("applyNodeTranslations sets aria-label from data-i18n-aria-label", () => {
  // Screen-reader labels must localize too (nav.menu / back-button labels).
  const n = fakeNode({ "data-i18n-aria-label": "nav.menu" });
  app.applyNodeTranslations(n, idTfn);
  assert.equal(n.getAttribute("aria-label"), "T(nav.menu)");
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
check("applyNodeTranslations writes data-i18n-html via innerHTML (keeps inline markup)", () => {
  // Hint paragraphs carry inline <strong>/<code> emphasis; a textContent write
  // would flatten them, so those nodes opt into data-i18n-html and their catalog
  // values carry the same tags.
  const n = fakeNode({ "data-i18n-html": "keys.hint" });
  n.innerHTML = "plain <code>--daemon-key</code>";
  app.applyNodeTranslations(n, (k) => `<strong>${k}</strong> <code>x</code>`);
  assert.equal(n.innerHTML, "<strong>keys.hint</strong> <code>x</code>");
  assert.equal(n.textContent, "", "data-i18n-html must not touch textContent");
});
check("applyNodeTranslations leaves data-i18n-html markup when the key is missing", () => {
  const n = fakeNode({ "data-i18n-html": "keys.hint" });
  n.innerHTML = "fallback <code>markup</code>";
  app.applyNodeTranslations(n, () => null);
  assert.equal(n.innerHTML, "fallback <code>markup</code>");
});
check("the markup-bearing hint catalog values keep their inline tags", () => {
  // Guard the pairing: index.html tags these two hints data-i18n-html, so both
  // catalogs must ship the inline <strong>/<code> fragments (a plain-text value
  // would silently strip the emphasis at boot).
  const html = fs.readFileSync(path.join(STATIC_DIR, "index.html"), "utf8");
  for (const key of ["keys.hint", "newTask.projectManualHint"]) {
    assert.ok(html.includes(`data-i18n-html="${key}"`), `${key} must be data-i18n-html`);
    for (const code of ["en-US", "zh-CN"]) {
      const dict = JSON.parse(
        fs.readFileSync(path.join(STATIC_DIR, "i18n", `${code}.json`), "utf8"));
      assert.match(dict[key], /<code>/, `${code}/${key} must keep its <code> markup`);
    }
  }
});
check("formatTokenUsage renders its labels from the active dictionary", () => {
  const savedLang = I18N.lang;
  const savedDicts = I18N.dicts;
  try {
    // Empty dicts (boot-time miss) → the English baseline string, unchanged.
    I18N.dicts = { "en-US": {}, "zh-CN": {} };
    I18N.lang = "zh-CN";
    assert.equal(
      app.formatTokenUsage({ input_tokens: 5 }),
      "in 5 · out 0 · cache r/w 0/0 · $0.0000");
    // With a dictionary loaded, the label chrome follows the selected language.
    I18N.dicts = {
      "en-US": { "usage.valueLine": "in {in} · out {out} · cache r/w {cacheRead}/{cacheWrite} · {cost}" },
      "zh-CN": { "usage.valueLine": "输入 {in} · 输出 {out} · 缓存 读/写 {cacheRead}/{cacheWrite} · {cost}" },
    };
    assert.equal(
      app.formatTokenUsage({ input_tokens: 1234, output_tokens: 567 }),
      "输入 1,234 · 输出 567 · 缓存 读/写 0/0 · $0.0000");
    I18N.lang = "en-US";
    assert.equal(
      app.formatTokenUsage({ input_tokens: 1234, output_tokens: 567 }),
      "in 1,234 · out 567 · cache r/w 0/0 · $0.0000");
  } finally {
    I18N.lang = savedLang;
    I18N.dicts = savedDicts;
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
// I18N.setLang — persistence + the post-switch re-render orchestration.
// A regression that drops the localStorage write (breaking persistence across
// reloads) or stops the onLangChange hook (leaving dynamic surfaces stuck in the
// old language) must fail here.
// ---------------------------------------------------------------------------
await checkAsync("setLang persists the chosen language to localStorage under STORAGE_KEY", async () => {
  const savedFetch = globalThis.fetch;
  const savedLS = globalThis.localStorage;
  const savedDicts = I18N.dicts;
  const savedLang = I18N.lang;
  const savedHook = I18N.onLangChange;
  try {
    const store = {};
    globalThis.localStorage = {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
    };
    globalThis.fetch = async () => ({ ok: true, json: async () => ({}) });
    I18N.dicts = { "en-US": {}, "zh-CN": {} };
    I18N.onLangChange = null;
    await I18N.setLang("zh-CN");
    assert.equal(I18N.lang, "zh-CN");
    assert.equal(store[I18N.STORAGE_KEY], "zh-CN",
      "setLang must write the chosen language under STORAGE_KEY");
    // An unsupported code is coerced to the fallback before it is persisted.
    await I18N.setLang("fr-FR");
    assert.equal(store[I18N.STORAGE_KEY], I18N.FALLBACK,
      "an unsupported code persists as the fallback, not verbatim");
  } finally {
    globalThis.fetch = savedFetch;
    if (savedLS === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = savedLS;
    I18N.dicts = savedDicts;
    I18N.lang = savedLang;
    I18N.onLangChange = savedHook;
  }
});

await checkAsync("setLang repaints static nodes and fires onLangChange after resolving", async () => {
  const savedFetch = globalThis.fetch;
  const savedLS = globalThis.localStorage;
  const savedDoc = globalThis.document;
  const savedDicts = I18N.dicts;
  const savedLang = I18N.lang;
  const savedHook = I18N.onLangChange;
  try {
    globalThis.localStorage = { getItem: () => null, setItem: () => {} };
    globalThis.fetch = async (url) => ({
      ok: true,
      json: async () => (String(url).includes("zh-CN")
        ? { "nav.history": "历史记录" }
        : { "nav.history": "History" }),
    });
    const node = fakeNode({ "data-i18n": "nav.history" });
    node.textContent = "History"; // in-markup English before the switch
    globalThis.document = {
      querySelectorAll(sel) {
        const attr = sel.slice(1, -1);
        return node.getAttribute(attr) != null ? [node] : [];
      },
    };
    I18N.dicts = { "en-US": {}, "zh-CN": {} };
    let hookCalls = 0;
    let langAtHook = null;
    let nodeAtHook = null;
    I18N.onLangChange = () => {
      hookCalls += 1;
      langAtHook = I18N.lang;
      nodeAtHook = node.textContent;
    };
    await I18N.setLang("zh-CN");
    // The static-translation pass repainted the tagged node in the new language.
    assert.equal(node.textContent, "历史记录",
      "applyStaticTranslations must repaint tagged nodes on a switch");
    // The re-render hook fired exactly once, AFTER the switch had resolved (so
    // it observes the new language and the already-repainted static nodes).
    assert.equal(hookCalls, 1, "onLangChange must fire exactly once per switch");
    assert.equal(langAtHook, "zh-CN", "the hook observes the new language");
    assert.equal(nodeAtHook, "历史记录",
      "onLangChange runs after the static pass, not before");
  } finally {
    globalThis.fetch = savedFetch;
    if (savedLS === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = savedLS;
    if (savedDoc === undefined) delete globalThis.document;
    else globalThis.document = savedDoc;
    I18N.dicts = savedDicts;
    I18N.lang = savedLang;
    I18N.onLangChange = savedHook;
  }
});

// ---------------------------------------------------------------------------
// Live-state chrome across a language switch: the connection badge and the tab
// title carry no data-i18n attribute (the static pass would clobber the badge's
// CURRENT status back to "connecting…"), so they repaint themselves.
// ---------------------------------------------------------------------------
check("repaintConnStatus re-renders the CURRENT status in the active language", () => {
  const savedDoc = globalThis.document;
  const savedDicts = I18N.dicts;
  const savedLang = I18N.lang;
  try {
    const badge = { className: "conn conn-connecting", textContent: "connecting…" };
    globalThis.document = { getElementById: (id) => (id === "conn-status" ? badge : null) };
    I18N.dicts = {
      "en-US": { "conn.connected": "connected" },
      "zh-CN": { "conn.connected": "已连接" },
    };

    I18N.lang = "en-US";
    app.setConnStatus("connected", "conn.connected", "connected");
    assert.equal(badge.textContent, "connected");
    assert.equal(badge.className, "conn conn-connected");

    // A language switch repaints the badge with the CURRENT status — not the
    // boot-time "connecting…" text — and keeps its state class.
    I18N.lang = "zh-CN";
    app.repaintConnStatus();
    assert.equal(badge.textContent, "已连接");
    assert.equal(badge.className, "conn conn-connected");
  } finally {
    if (savedDoc === undefined) delete globalThis.document;
    else globalThis.document = savedDoc;
    I18N.dicts = savedDicts;
    I18N.lang = savedLang;
  }
});

check("applyDocumentTitle localizes the browser tab title", () => {
  const savedDoc = globalThis.document;
  const savedDicts = I18N.dicts;
  const savedLang = I18N.lang;
  try {
    globalThis.document = { title: "SE3 Control Plane", getElementById: () => null };
    I18N.dicts = {
      "en-US": { "topbar.title": "SE3 Control Plane" },
      "zh-CN": { "topbar.title": "SE3 控制台" },
    };
    I18N.lang = "zh-CN";
    app.applyDocumentTitle();
    assert.equal(globalThis.document.title, "SE3 控制台");
    I18N.lang = "en-US";
    app.applyDocumentTitle();
    assert.equal(globalThis.document.title, "SE3 Control Plane");
  } finally {
    if (savedDoc === undefined) delete globalThis.document;
    else globalThis.document = savedDoc;
    I18N.dicts = savedDicts;
    I18N.lang = savedLang;
  }
});

// The enum <option> labels must be tagged for translation while their canonical
// value= attributes stay untranslated (they are the wire format).
check("index.html enum options are data-i18n tagged with untranslated values", () => {
  const html = fs.readFileSync(path.join(STATIC_DIR, "index.html"), "utf8");
  const en = JSON.parse(
    fs.readFileSync(path.join(STATIC_DIR, "i18n", "en-US.json"), "utf8"));
  const cases = [
    ["issueType", ["bug", "feature", "enhancement", "idea", "task"]],
    ["issuePriority", ["critical", "high", "medium", "low"]],
    ["taskType", ["feature", "bugfix", "review", "small", "directive"]],
  ];
  for (const [ns, values] of cases) {
    for (const v of values) {
      const key = `${ns}.${v}`;
      assert.ok(html.includes(`data-i18n="${key}"`), `index.html misses ${key}`);
      assert.ok(key in en, `en-US baseline misses ${key}`);
    }
  }
  // The live connection badge must NOT be static-translated (see repaint above).
  assert.ok(!/id="conn-status"[^>]*data-i18n=/.test(html),
    "#conn-status must not carry data-i18n — the static pass would clobber it");
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

// ---------------------------------------------------------------------------
// Language registry — driven by the server's /i18n/index.json manifest, so a
// newly dropped locale JSON is selectable without editing app.js.
// ---------------------------------------------------------------------------
check("parseManifest normalizes the {languages:[...]} shape", () => {
  const langs = I18N.parseManifest({
    languages: [
      { code: "en-US", label: "English" },
      { code: "fr-FR", label: "Français" },
    ],
  });
  assert.deepEqual(langs, [
    { code: "en-US", label: "English" },
    { code: "fr-FR", label: "Français" },
  ]);
});
check("parseManifest accepts a bare code array and force-includes en-US", () => {
  // en-US is the per-key fallback dictionary — it must always be loadable even
  // if a deployment's manifest omits it.
  const langs = I18N.parseManifest(["zh-CN", "ja-JP"]);
  assert.deepEqual(langs.map((l) => l.code), ["en-US", "zh-CN", "ja-JP"]);
  // A code with no label labels itself (the switcher never shows a blank).
  assert.equal(langs[1].label, "zh-CN");
});
check("parseManifest returns [] for unusable data", () => {
  assert.deepEqual(I18N.parseManifest(null), []);
  assert.deepEqual(I18N.parseManifest({}), []);
  assert.deepEqual(I18N.parseManifest({ languages: [1, {}, { label: "x" }] }), []);
});

await checkAsync("loadManifest adopts a server-registered language (no app.js edit)", async () => {
  const savedFetch = globalThis.fetch;
  const savedSup = I18N.SUPPORTED;
  const savedLabels = I18N.LABELS;
  const savedLang = I18N.lang;
  const savedDicts = I18N.dicts;
  const savedStore = globalThis.localStorage;
  try {
    globalThis.fetch = async (url) => {
      if (String(url).endsWith("/i18n/index.json")) {
        return {
          ok: true,
          json: async () => ({
            languages: [
              { code: "en-US", label: "English" },
              { code: "zh-CN", label: "中文" },
              { code: "fr-FR", label: "Français" },
            ],
          }),
        };
      }
      return { ok: true, json: async () => ({ "nav.history": "Historique" }) };
    };
    const store = {};
    globalThis.localStorage = {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = v; },
    };
    I18N.dicts = { "en-US": {}, "zh-CN": {} };

    await I18N.loadManifest();
    assert.deepEqual(I18N.SUPPORTED, ["en-US", "zh-CN", "fr-FR"]);
    assert.equal(I18N.LABELS["fr-FR"], "Français");
    // The new language now wins language resolution instead of falling back.
    assert.equal(
      I18N.resolveInitialLang(null, "fr-FR", I18N.SUPPORTED), "fr-FR");
    // ...and the switcher can select it: setLang no longer rejects it.
    await I18N.setLang("fr-FR");
    assert.equal(I18N.lang, "fr-FR");
    assert.equal(store[I18N.STORAGE_KEY], "fr-FR");
    assert.equal(I18N.t("nav.history"), "Historique");
  } finally {
    globalThis.fetch = savedFetch;
    if (savedStore === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = savedStore;
    I18N.SUPPORTED = savedSup;
    I18N.LABELS = savedLabels;
    I18N.lang = savedLang;
    I18N.dicts = savedDicts;
    I18N.onLangChange = null;
  }
});

await checkAsync("loadManifest keeps the bootstrap registry when the fetch fails", async () => {
  const savedFetch = globalThis.fetch;
  const savedSup = I18N.SUPPORTED.slice();
  try {
    globalThis.fetch = async () => { throw new Error("network down"); };
    const sup = await I18N.loadManifest();
    assert.deepEqual(sup, savedSup);
    assert.deepEqual(I18N.SUPPORTED, savedSup);
  } finally {
    globalThis.fetch = savedFetch;
    I18N.SUPPORTED = savedSup;
  }
});

// ---------------------------------------------------------------------------
// Dynamic display fallbacks — localized via tf(), with the in-code English
// literal as the built-in fallback when no dictionary is loaded.
// ---------------------------------------------------------------------------

function withDicts(lang, dicts, fn) {
  const savedLang = I18N.lang;
  const savedDicts = I18N.dicts;
  try {
    I18N.lang = lang;
    I18N.dicts = dicts;
    fn();
  } finally {
    I18N.lang = savedLang;
    I18N.dicts = savedDicts;
  }
}

check("issueDisplayTitle keeps the English literal when no dict is loaded", () => {
  // The pure/document-less path: an empty dict must not paint a raw dotted key.
  withDicts("en-US", { "en-US": {}, "zh-CN": {} }, () => {
    assert.equal(app.issueDisplayTitle({}), "untitled");
    assert.equal(app.issueDisplayTitle(null), "untitled");
  });
});

check("issueDisplayTitle localizes its untitled placeholder", () => {
  withDicts("zh-CN", {
    "en-US": { "issue.untitled": "untitled" },
    "zh-CN": { "issue.untitled": "未命名" },
  }, () => {
    assert.equal(app.issueDisplayTitle({}), "未命名");
    assert.equal(app.issueDisplayTitle({ title: "  ", description: " \n " }), "未命名");
    // Issue text itself is data — never translated.
    assert.equal(app.issueDisplayTitle({ title: "Fix login" }), "Fix login");
  });
});

check("chipLabel localizes the system-prompt chip of an empty-content user turn", () => {
  // The empty-user-content path builds its chip through chipLabel({role:"system"}),
  // so it must localize like every other chip rather than hardcoding English.
  withDicts("zh-CN", {
    "en-US": { "prompt.chipLabel": "{role} prompt · {ctx}" },
    "zh-CN": { "prompt.chipLabel": "{role} 提示词 · {ctx}" },
  }, () => {
    assert.equal(
      app.chipLabel({ role: "system", stepType: "discovery" }),
      "system 提示词 · discovery",
    );
  });
  withDicts("en-US", { "en-US": {}, "zh-CN": {} }, () => {
    assert.equal(
      app.chipLabel({ role: "system", stepType: "discovery" }),
      "system prompt · discovery",
    );
  });
});

check("flowStatusText localizes a known flow status and passes unknown ones through", () => {
  withDicts("zh-CN", {
    "en-US": { "status.flow.running": "running", "status.flow.unknown": "unknown" },
    "zh-CN": { "status.flow.running": "运行中", "status.flow.unknown": "未知" },
  }, () => {
    assert.equal(app.flowStatusText("running"), "运行中");
    assert.equal(app.flowStatusText("RUNNING"), "运行中");
    assert.equal(app.flowStatusText(""), "未知");
    assert.equal(app.flowStatusText(null), "未知");
    // A status this frontend does not know yet degrades to the raw token
    // rather than a raw dotted key.
    assert.equal(app.flowStatusText("quantum"), "quantum");
  });
  // No dictionary loaded: the raw token is the built-in fallback.
  withDicts("en-US", { "en-US": {}, "zh-CN": {} }, () => {
    assert.equal(app.flowStatusText("completed"), "completed");
    assert.equal(app.flowStatusText(undefined), "unknown");
  });
});

check("issueStatusText localizes issue statuses, including the hyphenated ones", () => {
  withDicts("zh-CN", {
    "en-US": {
      "status.issue.open": "open",
      "status.issue.inProgress": "in-progress",
      "status.issue.wontFix": "won't-fix",
    },
    "zh-CN": {
      "status.issue.open": "待处理",
      "status.issue.inProgress": "进行中",
      "status.issue.wontFix": "不修复",
    },
  }, () => {
    assert.equal(app.issueStatusText("open"), "待处理");
    assert.equal(app.issueStatusText("in-progress"), "进行中");
    assert.equal(app.issueStatusText("won't-fix"), "不修复");
    // Missing status defaults to open (matches issueStatusClass).
    assert.equal(app.issueStatusText(null), "待处理");
    assert.equal(app.issueStatusText("mystery"), "mystery");
  });
  withDicts("en-US", { "en-US": {}, "zh-CN": {} }, () => {
    assert.equal(app.issueStatusText("closed"), "closed");
    assert.equal(app.issueStatusText(undefined), "open");
  });
});

check("the flow-card current-step label renders through the catalog", () => {
  withDicts("zh-CN", {
    "en-US": { "flow.card.currentStep": "step: {step}" },
    "zh-CN": { "flow.card.currentStep": "步骤：{step}" },
  }, () => {
    assert.equal(app.I18N.t("flow.card.currentStep", { step: "analyze" }), "步骤：analyze");
  });
});

check("issue type / priority / source tokens render through the catalog", () => {
  withDicts("zh-CN", {
    "en-US": {
      "issueType.bug": "bug",
      "issuePriority.high": "high",
      "issueSource.system": "system",
      "issueSource.human": "human",
    },
    "zh-CN": {
      "issueType.bug": "缺陷",
      "issuePriority.high": "高",
      "issueSource.system": "系统",
      "issueSource.human": "人工",
    },
  }, () => {
    assert.equal(app.issueTypeText("bug"), "缺陷");
    assert.equal(app.issuePriorityText("high"), "高");
    assert.equal(app.issueSourceText("human"), "人工");
    // An absent source is presented as "system" (matches the card's CSS class).
    assert.equal(app.issueSourceText(undefined), "系统");
    // Unknown tokens pass through verbatim rather than as a raw catalog key.
    assert.equal(app.issueTypeText("chore"), "chore");
    assert.equal(app.issuePriorityText("blocker"), "blocker");
    assert.equal(app.issueSourceText("daemon"), "daemon");
    // Empty type/priority stay empty so the caller can skip the chip / show "-".
    assert.equal(app.issueTypeText(""), "");
    assert.equal(app.issuePriorityText(null), "");
  });
  withDicts("en-US", { "en-US": { "issueType.bug": "bug" }, "zh-CN": {} }, () => {
    assert.equal(app.issueTypeText("bug"), "bug");
  });
});

console.log(`\n${passed} i18n check(s) passed.`);
