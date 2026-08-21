"""Pytest bridge for the WebUI step-report renderer family (G1).

Three defects in one family, all in the ``step_completed`` report-card path of
``static/app.js``:

* the generic key/value renderer dumped the usage-metadata keys
  (``token_usage`` / ``usage_records`` / ``usage_summary``) as ordinary fields
  even though ``renderStepReport`` already surfaces them as the card's compact
  ``buildStepUsageFootnote`` line;
* ``confirm`` / ``invariant_check`` / ``adjudicate`` had no dedicated renderer
  and fell through that same generic dump — adjudicate's audit structures
  (candidate_verdicts / rejected_candidates / …) buried the ruling itself;
* ``invariant_check`` / ``adjudicate`` / ``e2e`` were missing from
  ``STEP_REPORT_TITLES``, so ``reportCardTitle`` degraded their card title to
  the raw step key.

The behavioural assertions live in
``tests/frontend/step_report_renderers.test.mjs``, which the Node assertion
harness ``tests/frontend/test_app_pure.mjs`` loads and runs. This module pulls
that suite into the pytest run, asserts the checks actually executed, and adds
static guardrails on the shipped assets (renderer registration, title entries,
and i18n key parity across both locale bundles).

The Node suite is skipped when ``node`` is not on PATH; it is still runnable by
hand via ``node tests/frontend/step_report_renderers.test.mjs``.
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
EN_JSON = STATIC_DIR / "i18n" / "en-US.json"
ZH_JSON = STATIC_DIR / "i18n" / "zh-CN.json"
HARNESS = REPO_ROOT / "tests" / "frontend" / "test_app_pure.mjs"
RENDERER_TEST = REPO_ROOT / "tests" / "frontend" / "step_report_renderers.test.mjs"

# Every user-visible string the three new cards render goes through i18n, so a
# key added to one bundle and forgotten in the other must fail here rather than
# silently painting English into a zh-CN console.
NEW_I18N_KEYS = [
    "stepReport.e2e",
    "stepReport.invariant_check",
    "stepReport.adjudicate",
    "stepReport.section.feedback",
    "stepReport.section.revisionFeedback",
    "stepReport.section.adjudicatedDescription",
    "stepReport.confirm.approved",
    "stepReport.confirm.revisionRequested",
    "stepReport.confirm.reviewer",
    "stepReport.confirm.reviewing",
    "stepReport.adjudicate.noop",
    "stepReport.adjudicate.ruled",
    "stepReport.adjudicate.contradictionType",
]


def test_renderer_module_present_and_registered_in_the_harness():
    assert RENDERER_TEST.is_file(), f"missing {RENDERER_TEST}"
    harness = HARNESS.read_text(encoding="utf-8")
    assert "step_report_renderers.test.mjs" in harness, (
        "step_report_renderers.test.mjs is not registered in test_app_pure.mjs"
    )
    assert "registerStepReportRendererTests" in harness


def test_app_js_registers_the_three_new_report_renderers():
    src = APP_JS.read_text(encoding="utf-8")
    for line in (
        "confirm: renderConfirmReport,",
        "invariant_check: renderInvariantCheckReport,",
        "adjudicate: renderAdjudicateReport,",
    ):
        assert line in src, f"{line!r} missing from STEP_REPORT_RENDERERS"


def test_app_js_excludes_usage_metadata_from_the_generic_dump():
    src = APP_JS.read_text(encoding="utf-8")
    assert "const USAGE_META_KEYS = new Set([" in src
    for key in ("token_usage", "usage_records", "usage_summary"):
        assert f'"{key}",' in src
    assert "Object.entries(outputs).filter(([k]) => !USAGE_META_KEYS.has(k))" in src, (
        "renderGenericOutputs must filter the usage-metadata keys"
    )


def test_app_js_title_map_covers_the_three_previously_missing_types():
    src = APP_JS.read_text(encoding="utf-8")
    for line in (
        'e2e: "E2E Scenarios",',
        'invariant_check: "Invariant Check",',
        'adjudicate: "Adjudication",',
    ):
        assert line in src, f"{line!r} missing from STEP_REPORT_TITLES"


def test_new_i18n_keys_exist_in_both_web_locale_bundles():
    en = json.loads(EN_JSON.read_text(encoding="utf-8"))
    zh = json.loads(ZH_JSON.read_text(encoding="utf-8"))
    for key in NEW_I18N_KEYS:
        assert key in en, f"{key} missing from en-US.json"
        assert key in zh, f"{key} missing from zh-CN.json"
    # en-US stays the baseline superset (the per-key fallback chain depends on it).
    assert [k for k in zh if k not in en] == []


def test_frontend_step_report_renderer_node_suite_passes():
    """Run the Node assertion suite and confirm the G1 checks actually ran."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")
    assert HARNESS.is_file(), f"missing {HARNESS}"
    result = subprocess.run(
        [node, str(HARNESS)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"frontend test runner exited {result.returncode}:\n{combined}"
    )
    for needle in (
        "usage keys: renderGenericOutputs drops them and keeps every other field",
        "usage keys: an outputs dict of NOTHING BUT usage metadata renders no kv block at all",
        "usage keys: renderDefaultReport falls to the empty state when only usage keys remain",
        "confirm: an approved verdict renders a ✓ status label, reviewer and reviewed step",
        "confirm: revision_feedback identical to feedback renders ONE section, not two",
        "invariant_check: issues render grouped by severity with the anchored issue schema",
        "invariant_check: diagnostic payloads stay out of the card",
        "adjudicate: a real ruling renders the type, rationale and adjudicated description",
        "adjudicate: the audit structures stay out of the card",
        "titles: invariant_check / adjudicate / e2e no longer degrade to the raw step key",
    ):
        assert needle in combined, (
            f"expected check {needle!r} in node output:\n{combined}"
        )
    assert "checks passed" in combined, combined
