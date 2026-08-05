/*
 * DOM-free tests for the INVESTIGATE step's WebUI labels (Group G6).
 *
 * The investigate step is new to the flow engine, so both label paths in app.js
 * had to gain an entry: `STEP_HEADER_TITLES` (the uppercase conversation
 * step-section heading) and `STEP_REPORT_TITLES` (the title-case report-card
 * base label). Both maps are only the OFFLINE fallback — the rendered label is
 * resolved through `I18N` at paint time. A step type present in the map but
 * absent from the catalogs would therefore look correct in a document-less test
 * and still render an English literal to a zh-CN user, so these checks drive the
 * label helpers with the SHIPPED dictionaries rather than hand-written stubs.
 *
 * Also covers the two properties an added map entry can quietly break: the
 * unknown-step-type fallback (which must keep degrading to the raw key so the
 * strict time order and separator rebuild survive), and the survey task type's
 * `taskType.*` catalog entry that landed alongside investigate.
 *
 * Run manually:  node tests/frontend/investigate_step_labels.test.mjs
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
const {
  I18N, STEP_HEADER_TITLES, STEP_REPORT_TITLES, STEP_RESULT_FIELDS,
  stepHeaderLabel, reportCardTitle, isStepResultDict,
} = app;

let passed = 0;
function check(name, fn) {
  fn();
  passed += 1;
  console.log("  ok -", name);
}

// The real shipped catalogs — the point of this suite is that the labels resolve
// against what actually ships, not against a stub that restates the assertion.
const SHIPPED = {};
for (const code of ["en-US", "zh-CN"]) {
  SHIPPED[code] = JSON.parse(
    fs.readFileSync(path.join(STATIC_DIR, "i18n", `${code}.json`), "utf8"));
}

function withLang(code, fn) {
  const savedLang = I18N.lang;
  const savedDicts = I18N.dicts;
  try {
    I18N.dicts = { "en-US": SHIPPED["en-US"], "zh-CN": SHIPPED["zh-CN"] };
    I18N.lang = code;
    fn();
  } finally {
    I18N.lang = savedLang;
    I18N.dicts = savedDicts;
  }
}

// ---------------------------------------------------------------------------
// 1. The catalogs carry the keys both label maps will look up
// ---------------------------------------------------------------------------
check("both catalogs carry the investigate step-label keys", () => {
  for (const code of ["en-US", "zh-CN"]) {
    for (const key of ["stepHeader.investigate", "stepReport.investigate"]) {
      const v = SHIPPED[code][key];
      assert.ok(typeof v === "string" && v.trim(), `${code} is missing ${key}`);
    }
  }
  // zh-CN must not simply echo the English literal — a copy-pasted baseline
  // value is the failure mode a bare presence check would wave through.
  assert.notEqual(
    SHIPPED["zh-CN"]["stepHeader.investigate"],
    SHIPPED["en-US"]["stepHeader.investigate"]);
  assert.notEqual(
    SHIPPED["zh-CN"]["stepReport.investigate"],
    SHIPPED["en-US"]["stepReport.investigate"]);
});

check("investigate is in both offline label maps", () => {
  // The map literals are what renders before the dictionaries land; a missing
  // entry sends stepHeaderLabel down the unknown-type branch entirely, so the
  // key would never be resolved even once the catalogs are loaded.
  assert.ok(STEP_HEADER_TITLES.investigate, "STEP_HEADER_TITLES lacks investigate");
  assert.ok(STEP_REPORT_TITLES.investigate, "STEP_REPORT_TITLES lacks investigate");
});

// ---------------------------------------------------------------------------
// 2. stepHeaderLabel — localized in both languages, never the raw key
// ---------------------------------------------------------------------------
check("stepHeaderLabel('investigate') is localized in en-US", () => {
  withLang("en-US", () => {
    const label = stepHeaderLabel("investigate", "investigate");
    assert.equal(label, SHIPPED["en-US"]["stepHeader.investigate"]);
    assert.notEqual(label, "investigate");
    assert.notEqual(label, "stepHeader.investigate");
  });
});

check("stepHeaderLabel('investigate') is localized in zh-CN", () => {
  withLang("zh-CN", () => {
    const label = stepHeaderLabel("investigate", "investigate");
    assert.equal(label, SHIPPED["zh-CN"]["stepHeader.investigate"]);
    assert.notEqual(label, STEP_HEADER_TITLES.investigate);
    assert.notEqual(label, "stepHeader.investigate");
  });
});

check("stepHeaderLabel normalizes the step-type case", () => {
  withLang("zh-CN", () => {
    assert.equal(
      stepHeaderLabel("INVESTIGATE", "INVESTIGATE"),
      SHIPPED["zh-CN"]["stepHeader.investigate"]);
  });
});

check("stepHeaderLabel('investigate') degrades to the map literal offline", () => {
  const savedLang = I18N.lang;
  const savedDicts = I18N.dicts;
  try {
    // Boot-time miss (dictionaries not yet fetched) — the English map literal
    // is the fallback, exactly as for every other known step type.
    I18N.dicts = { "en-US": {}, "zh-CN": {} };
    I18N.lang = "zh-CN";
    assert.equal(stepHeaderLabel("investigate", "investigate"), "INVESTIGATE");
  } finally {
    I18N.lang = savedLang;
    I18N.dicts = savedDicts;
  }
});

// ---------------------------------------------------------------------------
// 3. reportCardTitle — the report-card base label follows the language too
// ---------------------------------------------------------------------------
check("reportCardTitle('investigate') is localized in en-US", () => {
  withLang("en-US", () => {
    assert.equal(
      reportCardTitle("investigate"),
      `${SHIPPED["en-US"]["stepReport.investigate"]} · ${SHIPPED["en-US"]["stepReportSuffix.default"]}`);
  });
});

check("reportCardTitle('investigate') is localized in zh-CN", () => {
  withLang("zh-CN", () => {
    const title = reportCardTitle("investigate");
    assert.equal(
      title,
      `${SHIPPED["zh-CN"]["stepReport.investigate"]} · ${SHIPPED["zh-CN"]["stepReportSuffix.default"]}`);
    // Neither the raw step key nor the English base label leaks through.
    assert.ok(!title.includes("investigate"), title);
    assert.ok(!title.includes(STEP_REPORT_TITLES.investigate), title);
  });
});

// ---------------------------------------------------------------------------
// 4. The unknown-step-type fallback is untouched by the new entry
// ---------------------------------------------------------------------------
check("an unknown step type still falls back to the supplied label", () => {
  withLang("zh-CN", () => {
    // stepHeaderLabel keeps the caller's fallback so the strict time order and
    // separator rebuild survive a step type this build has never heard of.
    assert.equal(stepHeaderLabel("investigate_v2", "investigate_v2"), "investigate_v2");
    assert.equal(stepHeaderLabel("totally_unknown", "Totally Unknown"), "Totally Unknown");
    assert.equal(stepHeaderLabel("", "fallback"), "fallback");
  });
});

check("reportCardTitle degrades an unknown step type to its raw key", () => {
  withLang("zh-CN", () => {
    assert.equal(
      reportCardTitle("investigate_v2"),
      `investigate_v2 · ${SHIPPED["zh-CN"]["stepReportSuffix.default"]}`);
    // A blank step type still produces a title rather than throwing.
    assert.equal(
      reportCardTitle(""),
      `Step · ${SHIPPED["zh-CN"]["stepReportSuffix.default"]}`);
  });
});

// ---------------------------------------------------------------------------
// 4b. The result-field set — the third map an added step type needs
// ---------------------------------------------------------------------------
check("investigate has a STEP_RESULT_FIELDS entry", () => {
  // Without it isStepResultDict returns false for every investigate turn, so
  // the finished report is never recognized as the step's final result and the
  // thinking process stays expanded inline instead of collapsing.
  assert.ok(Array.isArray(STEP_RESULT_FIELDS.investigate),
    "STEP_RESULT_FIELDS lacks investigate");
  for (const key of ["root_cause", "evidence", "files_involved",
    "suggested_fix_direction", "confidence", "conclusive"]) {
    assert.ok(STEP_RESULT_FIELDS.investigate.includes(key),
      `STEP_RESULT_FIELDS.investigate lacks ${key}`);
  }
});

check("isStepResultDict recognizes an investigate report", () => {
  assert.equal(isStepResultDict("investigate", { root_cause: "x", conclusive: true }), true);
  // A genuine-but-negative verdict still counts: presence, not truthiness.
  assert.equal(isStepResultDict("investigate", { conclusive: false }), true);
  assert.equal(isStepResultDict("investigate", { files_involved: [] }), true);
  // An intermediate tool call carries none of the report keys.
  assert.equal(isStepResultDict("investigate", { command: "git log" }), false);
  assert.equal(isStepResultDict("investigate", { file_path: "src.py" }), false);
});

// ---------------------------------------------------------------------------
// 5. The survey task type shipped alongside investigate
// ---------------------------------------------------------------------------
check("both catalogs carry taskType.survey and no retired taskType.directive", () => {
  for (const code of ["en-US", "zh-CN"]) {
    const v = SHIPPED[code]["taskType.survey"];
    assert.ok(typeof v === "string" && v.trim(), `${code} is missing taskType.survey`);
    assert.ok(
      !("taskType.directive" in SHIPPED[code]),
      `${code} still carries the retired taskType.directive`);
  }
  // The five explicit task types all have a display label; discovery is the
  // --discover entry's flow shape and is deliberately not one of them.
  for (const type of ["feature", "bugfix", "review", "small", "survey"]) {
    assert.ok(
      SHIPPED["en-US"][`taskType.${type}`],
      `en-US baseline is missing taskType.${type}`);
  }
});

check("an unknown task type falls through to its raw string", () => {
  // Historical flows persisted `task_type: directive`; with the catalog key
  // gone the console must still show something, so resolve must return null and
  // the caller keep the raw value.
  withLang("zh-CN", () => {
    assert.equal(I18N.resolve("taskType.directive"), null);
    assert.equal(I18N.resolve("taskType.survey"), SHIPPED["zh-CN"]["taskType.survey"]);
  });
});

console.log(`\n${passed} investigate step-label check(s) passed.`);
