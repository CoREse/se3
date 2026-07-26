"""Pytest bridge for the web console's self_check status-bar fallback.

``renderSelfCheckReport`` is reached by two paths. A real step report carries
``step.outputs.actionable_count``, computed by ``self_check_handler`` after
``_validate_and_filter_issues``. An assistant message rendered inline in the
conversation instead reaches the same renderer through
``makeStructuredAssistantRenderer``, which passes the LLM's raw JSON as
synthetic outputs — and that JSON has no ``actionable_count``. The renderer used
to fall back to ``0`` there, hit its "no actionable issues" branch and paint a
green ``✓ PASSED`` directly above the issue list it went on to render.

The renderer now derives the count from ``outputs.issues`` when the key is
absent, wording that path neutrally (``✗ N issue(s)``) because the raw LLM
issues are unvalidated. The DOM assertions live in
``tests/frontend/self_check_passed_fallback.test.mjs``, which the Node harness
``tests/frontend/test_app_pure.mjs`` loads. This module pulls that suite into
the pytest run, asserts the regression check actually executed (so a deleted
registration cannot silently turn the suite into a no-op), and adds a static
source guardrail that the ``issues.length`` derivation is still in place.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_JS = REPO_ROOT / "src" / "tianluo" / "server" / "static" / "app.js"
FRONTEND_TEST = REPO_ROOT / "tests" / "frontend" / "test_app_pure.mjs"
SELF_CHECK_TEST = REPO_ROOT / "tests" / "frontend" / "self_check_passed_fallback.test.mjs"

REGRESSION_CHECK = (
    "self_check_passed_fallback: missing actionable_count with issues "
    "renders a failure label, never ✓ PASSED"
)


def test_self_check_passed_fallback_module_present():
    """The mjs module exists and is registered into the harness."""
    assert SELF_CHECK_TEST.is_file(), f"missing {SELF_CHECK_TEST}"
    harness = FRONTEND_TEST.read_text(encoding="utf-8")
    assert "self_check_passed_fallback.test.mjs" in harness, (
        "self_check_passed_fallback.test.mjs is not registered in test_app_pure.mjs"
    )
    assert "registerSelfCheckPassedFallbackTests" in harness


def test_self_check_passed_fallback_node_suite_passes():
    """Run the Node assertion suite and confirm the regression check ran.

    Skipped if ``node`` is not available on PATH; runnable by hand via
    ``node tests/frontend/test_app_pure.mjs``.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")
    assert FRONTEND_TEST.is_file(), f"missing {FRONTEND_TEST}"
    result = subprocess.run(
        [node, str(FRONTEND_TEST)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"frontend test runner exited {result.returncode}:\n{combined}"
    )
    for needle in (
        REGRESSION_CHECK,
        "self_check_passed_fallback: actionable_count 0 renders ✓ PASSED",
        "self_check_passed_fallback: status failed renders ✗ FAILED ahead of "
        "a would-be ✓ PASSED",
    ):
        assert needle in combined, (
            f"expected check {needle!r} in node output:\n{combined}"
        )
    assert "checks passed" in combined, combined


def test_self_check_renderer_derives_count_from_issues():
    """Guard the fix itself: the missing-key path must derive from issues.length.

    Pinned as source features rather than an absence of the old ``: 0`` literal,
    which would misfire on unrelated reformatting elsewhere in the file.
    """
    assert APP_JS.is_file(), f"missing {APP_JS}"
    js = APP_JS.read_text(encoding="utf-8")
    assert "const hasCount = outputs.actionable_count != null;" in js, (
        "renderSelfCheckReport must branch on whether actionable_count exists"
    )
    assert "hasCount ? Number(outputs.actionable_count) : issues.length" in js, (
        "a missing actionable_count must derive the count from issues.length, "
        "not fall back to 0"
    )
    assert "`✗ ${actionable} issue(s)`" in js, (
        "the derived path must use the neutral (non-'actionable') wording"
    )
    assert "`✗ ${actionable} actionable issue(s)`" in js, (
        "the actionable_count path must keep its original wording"
    )
