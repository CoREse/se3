"""Pytest bridge for the history view's flow_id + session token-usage display.

The webui history view surfaces three frontend-only enhancements (no backend /
data-model change):

  1. each history list card shows the session's ``flow_id`` in its meta row
     (``.history-item-flow-id``, ellipsis-truncated with a full-value title);
  2. the history-detail header shows the open session's COMPLETE ``flow_id`` on
     its own dedicated line (``#history-detail-flow-id``), independent of the
     title's ``task_description``→``flow_id`` fallback;
  3. the history-detail header carries a session token-usage badge
     (``#history-usage-badge``) that reuses the running-flow view's
     ``.flow-usage-badge`` styling and the shared ``applyUsageBadge`` renderer
     (backend payload first, explicit unavailable state otherwise).

The behavioural assertions for the DOM-stubbed render live in
``tests/frontend/history_flow_id.test.mjs`` and
``tests/frontend/history_usage.test.mjs``, which the Node assertion harness
``tests/frontend/test_app_pure.mjs`` loads and runs. This pytest module pulls
that suite into the pytest run, asserts the new checks actually executed, and
adds static-source guardrails that the new elements / CSS classes exist and the
new module is wired into the harness.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "src" / "tianluo" / "server" / "static"
APP_JS = STATIC_DIR / "app.js"
STYLE_CSS = STATIC_DIR / "style.css"
INDEX_HTML = STATIC_DIR / "index.html"
FRONTEND_TEST = REPO_ROOT / "tests" / "frontend" / "test_app_pure.mjs"
FLOW_ID_TEST = REPO_ROOT / "tests" / "frontend" / "history_flow_id.test.mjs"
USAGE_TEST = REPO_ROOT / "tests" / "frontend" / "history_usage.test.mjs"


def test_history_flow_id_modules_present_and_registered():
    """The new mjs modules exist and are wired into the harness."""
    assert FLOW_ID_TEST.is_file(), f"missing {FLOW_ID_TEST}"
    assert USAGE_TEST.is_file(), f"missing {USAGE_TEST}"
    harness = FRONTEND_TEST.read_text(encoding="utf-8")
    assert "history_flow_id.test.mjs" in harness, (
        "history_flow_id.test.mjs is not registered in test_app_pure.mjs"
    )
    assert "registerHistoryFlowIdTests" in harness
    assert "history_usage.test.mjs" in harness, (
        "history_usage.test.mjs is not registered in test_app_pure.mjs"
    )
    assert "registerHistoryUsageTests" in harness


def test_history_flow_id_node_suite_passes():
    """Run the Node assertion suite and confirm the new checks ran.

    Skipped if ``node`` is not available on PATH; the suite is still runnable by
    hand via ``node tests/frontend/test_app_pure.mjs``.
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
    # The new history flow_id / usage checks must have actually executed.
    for needle in (
        "history list meta row shows the session flow_id with a full-value title",
        "history detail shows the FULL flow_id even when a task_description exists",
        "closeHistory clears the flow_id line and hides the usage badge",
        "updateHistoryUsageBadge hides + clears the badge with no usage",
        "updateHistoryUsageBadge shows explicit unavailable state once usage exists",
        "history + flow badges render an identical value for the same records (shared helper)",
    ):
        assert needle in combined, (
            f"expected check {needle!r} in node output:\n{combined}"
        )
    assert "checks passed" in combined, combined


def test_history_flow_id_elements_wired_into_page():
    """index.html carries the dedicated flow_id line and the usage badge."""
    assert INDEX_HTML.is_file(), f"missing {INDEX_HTML}"
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="history-detail-flow-id"' in html, (
        "history-detail-flow-id element is not present in index.html"
    )
    assert 'id="history-usage-badge"' in html, (
        "history-usage-badge element is not present in index.html"
    )
    # The badge reuses the running-flow badge styling and is default-hidden so a
    # freshly-opened session with no usage shows nothing.
    idx = html.find('id="history-usage-badge"')
    tag = html[idx : html.find(">", idx)]
    assert "flow-usage-badge" in tag, (
        "history-usage-badge must reuse the .flow-usage-badge class"
    )
    assert "hidden" in tag, "history-usage-badge must be default-hidden"


def test_history_flow_id_css_present():
    """The list-card and detail-line flow_id selectors must exist."""
    assert STYLE_CSS.is_file(), f"missing {STYLE_CSS}"
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".history-item-flow-id" in css, (
        "history-item-flow-id CSS (list-card meta span) is missing"
    )
    assert ".history-detail-flow-id" in css, (
        "history-detail-flow-id CSS (detail-header line) is missing"
    )
    # The detail badge reuses the existing .flow-usage-badge styling (incl. its
    # .hidden suppression) — no new badge base style is introduced.
    assert ".flow-usage-badge.hidden" in css, (
        "flow-usage-badge must retain its .hidden rule for empty-usage suppression"
    )


def test_history_usage_badge_shares_flow_renderer():
    """The history badge must delegate to the shared applyUsageBadge helper.

    The task requires the history view reuse the running-flow view's exact
    rendering logic rather than a divergent copy, so both updaters delegate to
    one helper.
    """
    assert APP_JS.is_file(), f"missing {APP_JS}"
    js = APP_JS.read_text(encoding="utf-8")
    assert "function applyUsageBadge(" in js, (
        "the shared applyUsageBadge helper is missing"
    )
    assert "function updateHistoryUsageBadge(" in js, (
        "updateHistoryUsageBadge is missing"
    )
    # Both updaters delegate to the shared helper (no copied accumulate/format
    # logic that could drift between the two views).
    assert "applyUsageBadge($(\"flow-usage-badge\")" in js, (
        "updateFlowUsageBadge must delegate to applyUsageBadge"
    )
    assert "applyUsageBadge($(\"history-usage-badge\")" in js, (
        "updateHistoryUsageBadge must delegate to applyUsageBadge"
    )
