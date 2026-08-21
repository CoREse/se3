"""Pytest bridge for the WebUI's per-round test-result rendering (G2).

The test step writes one synthetic history record per fix round
(``steps/test.py:_record_test_history`` → ``chat_history.record_response``).
The payload is ``{overall_passed, phases: [{name, passed, returncode,
stdout_tail, stderr_tail}]}``; it is not a Claude stream line, so
``extract_assistant_text`` yields ``""`` and the record reached the console with
an empty body — rendered as "(no readable content for this record)". A FAILED
round compounded it: it returns REVISION_NEEDED, which is non-terminal, so no
``step_completed`` card is emitted for it at all, leaving the asymmetry "a
passing run has a card, a failing run has nothing".

The fix is frontend-only — the engine's write format is untouched, so it applies
retroactively to every history record already on disk. The behavioural
assertions live in ``tests/frontend/test_round_render.test.mjs``, which the Node
assertion harness ``tests/frontend/test_app_pure.mjs`` loads and runs. This
module pulls that suite into the pytest run, asserts the checks actually
executed, and adds static guardrails on the shipped assets (the shared helper's
two call sites and i18n key parity across both locale bundles).

The Node suite is skipped when ``node`` is not on PATH; it is still runnable by
hand via ``node tests/frontend/test_app_pure.mjs``.
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
ROUND_TEST = REPO_ROOT / "tests" / "frontend" / "test_round_render.test.mjs"

# Every user-visible string the round card renders goes through i18n, so a key
# added to one bundle and forgotten in the other must fail here rather than
# silently painting English into a zh-CN console.
NEW_I18N_KEYS = [
    "stepReport.test.roundTitle",
    "stepReport.test.returncode",
    "stepReport.test.failureSummary",
    "stepReport.test.stdoutTail",
    "stepReport.test.stderrTail",
]


def test_round_render_module_present_and_registered_in_the_harness():
    assert ROUND_TEST.is_file(), f"missing {ROUND_TEST}"
    harness = HARNESS.read_text(encoding="utf-8")
    assert "test_round_render.test.mjs" in harness, (
        "test_round_render.test.mjs is not registered in test_app_pure.mjs"
    )
    assert "registerTestRoundRenderTests" in harness


def test_app_js_renders_the_round_card_before_the_empty_state():
    """The recognizer must be consulted ahead of the '(no readable content)' fallback."""
    src = APP_JS.read_text(encoding="utf-8")
    card_at = src.find("const testRound = renderTestRoundCard(norm);")
    empty_at = src.find('tf("conv.recordEmpty"')
    assert card_at != -1, "renderConversationRecord does not consult renderTestRoundCard"
    assert empty_at != -1
    assert card_at < empty_at, (
        "the round card must be tried before the conv.recordEmpty empty state"
    )


def test_app_js_shares_one_phase_builder_between_both_test_cards():
    """One helper, two call sites — the streaming card and the terminal card."""
    src = APP_JS.read_text(encoding="utf-8")
    assert "function renderTestPhaseItem(" in src
    assert "function renderTestPhaseFailureDetail(" in src
    assert "function extractTestFailureSummary(" in src
    # renderTestRoundBody (streaming) and renderTestReport (step_completed).
    assert src.count("reportList(phases, renderTestPhaseItem)") == 2, (
        "both test cards must build their phase rows through the same helper"
    )


def test_app_js_round_card_reuses_the_existing_report_primitives():
    """No bespoke visual: the round card is built from the step-report family."""
    src = APP_JS.read_text(encoding="utf-8")
    start = src.find("function renderTestRoundBody(")
    end = src.find("function renderTestReport(")
    assert 0 < start < end
    body = src[start:end]
    for primitive in ("reportStatusBar(", "reportSection(", "reportList(",
                      "makeReportCard("):
        assert primitive in body, f"{primitive} not reused by the round card"
    assert "step-report__label " in body
    # Colours come from the shared step-report CSS, never from inline styles.
    assert "style." not in body


def test_new_i18n_keys_exist_in_both_web_locale_bundles():
    en = json.loads(EN_JSON.read_text(encoding="utf-8"))
    zh = json.loads(ZH_JSON.read_text(encoding="utf-8"))
    for key in NEW_I18N_KEYS:
        assert key in en, f"{key} missing from en-US.json"
        assert key in zh, f"{key} missing from zh-CN.json"
        assert zh[key] != "", f"{key} is empty in zh-CN.json"
    # `{code}` is the only interpolation slot; both bundles must keep it.
    for bundle in (en, zh):
        assert "{code}" in bundle["stepReport.test.returncode"]
    # en-US stays the baseline superset (the per-key fallback chain depends on it).
    assert [k for k in zh if k not in en] == []


def test_frontend_test_round_render_node_suite_passes():
    """Run the Node assertion suite and confirm the G2 checks actually ran."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")
    assert HARNESS.is_file(), f"missing {HARNESS}"
    result = subprocess.run(
        [node, str(HARNESS)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=180,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"frontend test runner exited {result.returncode}:\n{combined}"
    )
    for needle in (
        "test round: a synthetic test payload is recognized on an empty assistant record",
        "test round: an ordinary empty assistant record is NOT mistaken for one",
        "failure summary: pytest tail yields the banner first, then the failure rows",
        "failure summary: an unrecognized tail falls back to its last non-empty lines",
        "round card: a failing round renders FAILED, the exit code and the failure headline",
        "round card: a long tail folds, and the headline stays outside the fold",
        "stream: a synthetic test record renders the card instead of the empty state",
        "stream: a genuinely empty assistant record still shows the empty state",
        "terminal card: renderTestReport renders a failed phase through the shared helper",
    ):
        assert needle in combined, (
            f"expected check {needle!r} in node output:\n{combined}"
        )
    assert "checks passed" in combined, combined


def test_engine_write_format_is_unchanged_so_old_history_renders():
    """The recognizer keys off exactly what ``_record_test_history`` writes.

    This fix is retroactive only as long as the engine keeps emitting the same
    payload; if the writer's field names move, the frontend recognizer must move
    with them, and this guard is what says so.
    """
    src = (REPO_ROOT / "src" / "tianluo" / "engine" / "steps" / "test.py").read_text(
        encoding="utf-8")
    start = src.find("def _record_test_history(")
    assert start != -1
    body = src[start:start + 4000]
    assert '"overall_passed": overall_passed,' in body
    assert '"phases": [' in body
    assert '["stdout_tail"] = stdout_tail' in body
    assert '["stderr_tail"] = stderr_tail' in body
