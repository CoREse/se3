"""Pytest bridge for the web console's token-usage display (Group G4).

The running-flow console surfaces per-step token / cost usage as a low-key
footnote on each completed step's report card (`.step-report__usage`) and a
session-total badge docked in the flow-view corner (`.flow-usage-badge`),
accumulated client-side from the `token_usage` dicts the engine attaches to
`step.outputs` (G2). The data rides the existing per-step jsonl, so no
daemon/server protocol change is needed.

The behavioural assertions for the DOM-free pure helpers
(`formatTokenUsage` / `accumulateSessionUsage` / `isTokenUsageEmpty`) and the
DOM-stubbed footnote / badge render live in
`tests/frontend/token_usage.test.mjs`, which the Node assertion harness
`tests/frontend/test_app_pure.mjs` loads and runs. This pytest module pulls
that suite into the pytest run and asserts the G4 checks actually executed, and
adds static-source guardrails that the footnote / badge CSS exists and the
badge element is wired into the page.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "src" / "se3" / "server" / "static"
APP_JS = STATIC_DIR / "app.js"
STYLE_CSS = STATIC_DIR / "style.css"
INDEX_HTML = STATIC_DIR / "index.html"
FRONTEND_TEST = REPO_ROOT / "tests" / "frontend" / "test_app_pure.mjs"
TOKEN_USAGE_TEST = REPO_ROOT / "tests" / "frontend" / "token_usage.test.mjs"


def test_token_usage_module_present():
    """The G4 mjs module exists and is wired into the harness."""
    assert TOKEN_USAGE_TEST.is_file(), f"missing {TOKEN_USAGE_TEST}"
    harness = FRONTEND_TEST.read_text(encoding="utf-8")
    assert "token_usage.test.mjs" in harness, (
        "token_usage.test.mjs is not registered in test_app_pure.mjs"
    )
    assert "registerTokenUsageTests" in harness


def test_frontend_token_usage_node_suite_passes():
    """Run the Node assertion suite and confirm the G4 token-usage checks ran.

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
    # The token-usage checks must have actually executed (format + accumulate +
    # render footnote + badge), not silently been skipped.
    for needle in (
        "G4 formatTokenUsage renders labelled, comma-grouped fields",
        "G4 accumulateSessionUsage de-dups by step_id (no double count)",
        "G4 report card shows a usage footnote when the step has usage",
        "G4 updateFlowUsageBadge shows + populates the badge once usage exists",
    ):
        assert needle in combined, (
            f"expected G4 check {needle!r} in node output:\n{combined}"
        )
    assert "checks passed" in combined, combined


def test_token_usage_css_present():
    """The footnote and badge selectors must exist with low-key styling."""
    assert STYLE_CSS.is_file(), f"missing {STYLE_CSS}"
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".step-report__usage" in css, "step-report__usage CSS is missing"
    assert ".flow-usage-badge" in css, "flow-usage-badge CSS is missing"
    # The badge must have a hidden state so it can be suppressed when usage is
    # zero (acceptance: "无消耗时不显示").
    assert ".flow-usage-badge.hidden" in css, (
        "flow-usage-badge must have a .hidden rule to suppress empty usage"
    )


def test_token_usage_badge_wired_into_page():
    """The session-usage badge element exists in index.html (default-hidden)."""
    assert INDEX_HTML.is_file(), f"missing {INDEX_HTML}"
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="flow-usage-badge"' in html, (
        "flow-usage-badge element is not present in index.html"
    )
    # Default-hidden so a freshly-opened flow with no usage shows nothing.
    idx = html.find('id="flow-usage-badge"')
    tag = html[idx : html.find(">", idx)]
    assert "hidden" in tag, "flow-usage-badge must be default-hidden"
