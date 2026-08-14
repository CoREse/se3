"""Pytest bridge for the WebUI i18n subsystem (Group G6).

The web console gains a per-user UI-language subsystem: ``static/i18n/*.json``
locale dictionaries, an ``I18N`` object in ``app.js`` (resolveInitialLang /
lookup / t / load / applyStaticTranslations / setLang), ``data-i18n`` attribute
annotations across ``index.html``, and a top-bar language-switch control.

The behavioural assertions for the DOM-free + DOM-stub pure logic live in the
standalone Node suite ``tests/frontend/i18n_render_switch.test.mjs`` (same
pattern as ``tests/frontend/flow_resume.test.mjs`` /
``tests/frontend/end_session.test.mjs``). This pytest module:
  1. runs that Node suite and asserts the key checks actually executed;
  2. statically guards that the shipped assets carry the expected structure —
     both locale files exist and parse, ``en-US`` is the key superset,
     ``app.js`` carries + exports the subsystem, ``index.html`` carries the
     switch control and ``data-i18n`` annotations (with English fallback text),
     and ``style.css`` styles the switch inside + outside the mobile breakpoint.

The Node suite is skipped when ``node`` is not on PATH; it is still runnable by
hand via ``node tests/frontend/i18n_render_switch.test.mjs``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "src" / "tianluo" / "server" / "static"
APP_JS = STATIC_DIR / "app.js"
STYLE_CSS = STATIC_DIR / "style.css"
INDEX_HTML = STATIC_DIR / "index.html"
I18N_DIR = STATIC_DIR / "i18n"
EN_JSON = I18N_DIR / "en-US.json"
ZH_JSON = I18N_DIR / "zh-CN.json"
I18N_TEST = REPO_ROOT / "tests" / "frontend" / "i18n_render_switch.test.mjs"


# ---------------------------------------------------------------------------
# 1. Locale resource files
# ---------------------------------------------------------------------------
def test_locale_files_exist_and_parse():
    for path in (EN_JSON, ZH_JSON):
        assert path.is_file(), f"missing locale file {path}"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict) and data, f"{path} is not a non-empty object"


def test_en_us_is_the_baseline_key_superset():
    """en-US holds the key全集; every zh-CN key must exist in en-US so the
    per-key fallback chain (selected → en-US → key) always has a baseline."""
    en = json.loads(EN_JSON.read_text(encoding="utf-8"))
    zh = json.loads(ZH_JSON.read_text(encoding="utf-8"))
    missing = [k for k in zh if k not in en]
    assert missing == [], f"zh-CN keys absent from en-US baseline: {missing}"
    # A representative spread of namespaces must be present in the baseline.
    for key in (
        "nav.history", "login.title", "flows.title", "history.title",
        "issues.title", "newTask.title", "keys.title", "users.title",
        "flow.replySubmit", "common.cancel", "lang.en-US", "lang.zh-CN",
    ):
        assert key in en, f"en-US baseline is missing expected key {key!r}"


def test_endonym_labels_match_across_dicts():
    en = json.loads(EN_JSON.read_text(encoding="utf-8"))
    zh = json.loads(ZH_JSON.read_text(encoding="utf-8"))
    for code in ("en-US", "zh-CN"):
        assert en[f"lang.{code}"] == zh[f"lang.{code}"], (
            f"endonym lang.{code} must be identical across dictionaries"
        )


# ---------------------------------------------------------------------------
# 2. app.js carries + exports the subsystem
# ---------------------------------------------------------------------------
def test_app_js_has_i18n_subsystem():
    js = APP_JS.read_text(encoding="utf-8")
    for token in (
        "const I18N = {",
        "resolveInitialLang(",
        "applyStaticTranslations(",
        "function applyNodeTranslations(",
        "function initI18n(",
        "se3_ui_lang",  # the localStorage key
        "/i18n/",  # the fetch path (served from the root static mount)
    ):
        assert token in js, f"app.js is missing the i18n token {token!r}"


def test_app_js_exports_i18n_helpers():
    js = APP_JS.read_text(encoding="utf-8")
    start = js.find("module.exports")
    assert start != -1, "app.js has no module.exports block"
    exports = js[start:]
    for name in ("I18N", "applyNodeTranslations"):
        assert name in exports, f"{name} is not exported for the pure tests"


# ---------------------------------------------------------------------------
# 3. index.html — switch control + data-i18n annotations (English fallback)
# ---------------------------------------------------------------------------
def test_index_html_has_language_switch_and_annotations():
    html = INDEX_HTML.read_text(encoding="utf-8")
    # The switch control lives in the top bar and is NOT auth-only (so it is
    # usable on the login gate).
    assert 'id="lang-select"' in html, "index.html is missing the language switch"
    assert 'class="lang-select"' in html
    # A representative spread of data-i18n annotations across the major regions,
    # each keeping its English fallback text in-markup.
    for attr in (
        'data-i18n="nav.history"',
        'data-i18n="login.title"',
        'data-i18n="machines.empty"',
        'data-i18n="flow.replySubmit"',
        'data-i18n="newTask.submit"',
        'data-i18n-placeholder="flow.replyPlaceholder"',
        'data-i18n-title="flow.usageTitle"',
        'data-i18n="common.staleBanner"',
    ):
        assert attr in html, f"index.html is missing the annotation {attr}"
    # English fallback text is retained in-markup (JS-load-failure resilience).
    assert ">History<" in html
    assert ">Sign in<" in html


def test_index_html_annotations_reference_known_keys():
    """Every data-i18n* key used in index.html must exist in the en-US baseline
    (otherwise applyStaticTranslations would silently paint the raw key)."""
    import re

    html = INDEX_HTML.read_text(encoding="utf-8")
    en = json.loads(EN_JSON.read_text(encoding="utf-8"))
    keys = re.findall(r'data-i18n(?:-placeholder|-title)?="([^"]+)"', html)
    assert keys, "no data-i18n annotations found in index.html"
    unknown = sorted({k for k in keys if k not in en})
    assert unknown == [], f"index.html references keys absent from en-US: {unknown}"


# ---------------------------------------------------------------------------
# 4. style.css — switch styling, desktop + mobile
# ---------------------------------------------------------------------------
def test_style_css_styles_the_language_switch():
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".lang-select {" in css, "style.css is missing the .lang-select rule"
    # The mobile breakpoint must carry a .lang-select adaptation so the switch
    # stays usable on a phone without breaking the compact top bar.
    open_tok = "@media (max-width: 600px) {"
    start = css.index(open_tok)
    depth = 0
    j = css.index("{", start)
    end = j
    for k in range(j, len(css)):
        if css[k] == "{":
            depth += 1
        elif css[k] == "}":
            depth -= 1
            if depth == 0:
                end = k
                break
    block = css[start:end]
    assert ".lang-select" in block, (
        ".lang-select must have a mobile-breakpoint adaptation rule"
    )


# ---------------------------------------------------------------------------
# 4b. G10 strategy / scope / usage keys (completeness + translation)
# ---------------------------------------------------------------------------
G10_WEBUI_KEYS = [
    "newTask.strategy",
    "issueLaunch.strategy",
    "strategy.label",
    "strategy.reasonLabel",
    "strategy.requestedLabel",
    "strategy.inferredNote",
    "strategy.option.projectDefault",
    "strategy.option.auto",
    "strategy.option.direct",
    "strategy.option.planned",
    "strategy.value.auto",
    "strategy.value.direct",
    "strategy.value.planned",
    "strategy.value.not_applicable",
    "strategy.value.unknown",
    "scope.label",
    "scope.mode.full",
    "scope.mode.incremental",
    "scope.round.line",
    "scope.baseline",
    "scope.changedPaths",
    "scope.fullRounds",
    "usage.title",
    "usage.totalsLine",
    "usage.status.available",
    "usage.status.partial",
    "usage.status.unavailable",
    "usage.status.legacy_ambiguous",
    "usage.completeness",
    "usage.completeness.complete",
    "usage.completeness.partial",
    "usage.completeness.none",
    "usage.actual",
    "usage.estimated",
    "usage.unknown",
    "usage.unknownCalls",
    "usage.unknownModel",
    "usage.unknownPrice",
    "usage.unknownCacheTtl",
    "usage.flowHeader",
    "usage.callsHeader",
    "usage.stepsHeader",
    "usage.col.call",
    "usage.col.agent",
    "usage.col.runner",
    "usage.col.provider",
    "usage.col.model",
    "usage.col.status",
    "usage.col.input",
    "usage.col.output",
    "usage.col.cacheRead",
    "usage.col.cacheCreate",
    "usage.col.actual",
    "usage.col.estimate",
    "usage.col.step",
    "usage.col.calls",
    "usage.col.completeness",
    "usage.legacyNote",
    "usage.partialNote",
    "usage.noUsage",
]


def _webui_dicts():
    import json

    en = json.loads(EN_JSON.read_text(encoding="utf-8"))
    zh = json.loads(ZH_JSON.read_text(encoding="utf-8"))
    return en, zh


def test_g10_webui_keys_exist_in_en_us():
    en, _ = _webui_dicts()
    missing = [k for k in G10_WEBUI_KEYS if k not in en]
    assert not missing, f"en-US is missing G10 keys: {missing}"


def test_g10_webui_keys_translated_in_zh_cn():
    """zh-CN must provide semantic translations for the G10 keys (the en-US
    values would otherwise be served via fallback, which is legal but leaves
    the console half-translated)."""
    en, zh = _webui_dicts()
    missing = [k for k in G10_WEBUI_KEYS if k not in zh]
    assert not missing, f"zh-CN is missing G10 keys: {missing}"
    # Config tokens / product nouns may legitimately be identical across
    # languages (auto/direct/planned are strategy values, full/incremental are
    # scope-mode tokens, "Agent"/"Runner"/"Provider" are product nouns); prose
    # labels must be genuinely translated.
    token_keys = {
        "strategy.value.auto",
        "strategy.value.direct",
        "strategy.value.planned",
        "usage.totalsLine",
        "usage.col.agent",
        "usage.col.runner",
        "usage.col.provider",
    }
    raw = [
        k for k in G10_WEBUI_KEYS
        if k not in token_keys and (zh[k] == k or zh[k] == en[k])
    ]
    assert not raw, f"zh-CN G10 prose keys must be translated, not copied: {raw}"


def test_g10_webui_usage_status_keys_mirror_backend_values():
    """The usage.status.* label keys must cover exactly the backend UsageStatus
    values the payload can carry — a missing one renders the raw status token."""
    en, _ = _webui_dicts()
    for status in ("available", "partial", "unavailable", "legacy_ambiguous"):
        assert en.get(f"usage.status.{status}"), (
            f"missing usage.status.{status} label"
        )


# ---------------------------------------------------------------------------
# 5. Node suite — the pure helpers actually run and pass
# ---------------------------------------------------------------------------
def test_i18n_node_module_present():
    assert I18N_TEST.is_file(), f"missing {I18N_TEST}"


def test_frontend_i18n_node_suite_passes():
    """Run the Node assertion suite and confirm the i18n checks ran.

    Skipped if ``node`` is not available on PATH; still runnable by hand via
    ``node tests/frontend/i18n_render_switch.test.mjs``.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")
    result = subprocess.run(
        [node, str(I18N_TEST)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"i18n test runner exited {result.returncode}:\n{combined}"
    )
    for needle in (
        "resolveInitialLang honors a stored exact match first",
        "resolveInitialLang prefix-matches the navigator primary subtag",
        "resolveInitialLang falls back to en-US for an unknown navigator lang",
        "lookup falls back to the baseline dict for a missing key",
        "lookup returns the key itself when neither dict has it",
        "lookup interpolates {name} placeholders from params",
        "I18N.t resolves against the active language with en-US fallback",
        "applyNodeTranslations sets textContent from data-i18n",
        "applyStaticTranslations translates every tagged node in a scope",
        "applyStaticTranslations preserves in-markup text when the key is missing",
        "I18N.load degrades a failed fetch to an empty dict (no throw)",
        "en-US is the baseline: every zh-CN key exists in en-US",
    ):
        assert needle in combined, (
            f"expected i18n check {needle!r} in node output:\n{combined}"
        )
