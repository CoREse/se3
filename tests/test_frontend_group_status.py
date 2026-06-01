"""Pytest bridge for the web console's per-group DAG status markers (Group G4).

The running-flow console renders per-group `group_status` records — emitted by
the implement step's DAGScheduler lifecycle hooks via
`chat_history.record_group_status` — as lightweight, time-ordered status
markers ("G3 正在 worktree 实施中" / "G1 已完成") so the user can watch G1–G5
progress while the parallel implement step is still running.

The behavioural assertions for the DOM-free pure helpers
(`normalizeRecord` recognizing `type:'group_status'`, the `groupStatusLabel`
status→text mapping) and the DOM-stubbed marker render live in
`tests/frontend/group_status.test.mjs`, which the Node assertion harness
`tests/frontend/test_app_pure.mjs` loads and runs. This pytest module pulls
that suite into the pytest run and asserts the G4 checks actually executed, and
adds a static-source guardrail that the `.group-status-marker` CSS provides a
distinguishable running / completed / failed visual.
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
FRONTEND_TEST = REPO_ROOT / "tests" / "frontend" / "test_app_pure.mjs"
GROUP_STATUS_TEST = REPO_ROOT / "tests" / "frontend" / "group_status.test.mjs"


def test_group_status_module_present():
    """The registrable G4 mjs module exists and is wired into the harness."""
    assert GROUP_STATUS_TEST.is_file(), f"missing {GROUP_STATUS_TEST}"
    harness = FRONTEND_TEST.read_text(encoding="utf-8")
    assert "group_status.test.mjs" in harness, (
        "group_status.test.mjs is not registered in test_app_pure.mjs"
    )
    assert "registerGroupStatusTests" in harness


def test_frontend_group_status_node_suite_passes():
    """Run the Node assertion suite and confirm the G4 group_status checks ran.

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
    # The group_status checks must have actually executed (normalize + label +
    # render), not silently been skipped.
    for needle in (
        "G4 normalizeRecord recognizes group_status",
        "G4 groupStatusLabel covers every status",
        "G4 group_status renders a .group-status-marker",
    ):
        assert needle in combined, (
            f"expected G4 check {needle!r} in node output:\n{combined}"
        )
    assert "checks passed" in combined, combined


def test_group_status_css_distinguishes_running_completed_failed():
    """`.group-status-marker` must visually distinguish running/completed/failed.

    The running-flow-console contract requires the markers be "样式清晰可辨
    (running/completed/failed 视觉区分)". A cheap static guard: each of the
    three state selectors must set a distinct `border-left-color`.
    """
    assert STYLE_CSS.is_file(), f"missing {STYLE_CSS}"
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".group-status-marker" in css, "group-status-marker CSS is missing"
    colors = {}
    for status in ("running", "completed", "failed"):
        marker = f".group-status-marker.status-{status}"
        idx = css.find(marker)
        assert idx != -1, f"missing CSS rule for {marker}"
        block = css[idx : css.find("}", idx)]
        line = next(
            (ln for ln in block.splitlines() if "border-left-color" in ln),
            None,
        )
        assert line is not None, f"{marker} must set border-left-color"
        colors[status] = line.strip()
    assert len(set(colors.values())) == 3, (
        f"running/completed/failed must use distinct colors, got {colors}"
    )
