"""Pytest bridge for the web console's code-index update-progress markers (G3).

The commit step rebuilds ``tianluo/code-index.md`` before staging, re-summarising
every touched source node; that rebuild emits one ``type:'index_progress'``
NDJSON line per file/dir node via ``chat_history.record_index_progress``. The
running-flow console renders these as a single live "更新 code-index：<path>
(i/N)" progress line that updates in place as the counts climb.

The behavioural assertions for the DOM-free pure helpers
(``normalizeRecord`` recognizing ``type:'index_progress'``, the
``indexProgressLabel`` / ``indexProgressState`` mappings) and the DOM-stubbed
marker render + in-place convergence live in
``tests/frontend/index_progress.test.mjs``, which the Node assertion harness
``tests/frontend/test_app_pure.mjs`` loads and runs. This pytest module pulls
that suite into the pytest run and asserts the G3 checks actually executed, and
adds a static-source guardrail that the ``.index-progress-marker`` CSS provides
a distinguishable running / completed visual.
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
FRONTEND_TEST = REPO_ROOT / "tests" / "frontend" / "test_app_pure.mjs"
INDEX_PROGRESS_TEST = REPO_ROOT / "tests" / "frontend" / "index_progress.test.mjs"


def test_index_progress_module_present():
    """The registrable G3 mjs module exists and is wired into the harness."""
    assert INDEX_PROGRESS_TEST.is_file(), f"missing {INDEX_PROGRESS_TEST}"
    harness = FRONTEND_TEST.read_text(encoding="utf-8")
    assert "index_progress.test.mjs" in harness, (
        "index_progress.test.mjs is not registered in test_app_pure.mjs"
    )
    assert "registerIndexProgressTests" in harness


def test_frontend_index_progress_node_suite_passes():
    """Run the Node assertion suite and confirm the G3 index_progress checks ran.

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
    # The index_progress checks must have actually executed (normalize + label +
    # state + render + in-place convergence), not silently been skipped.
    for needle in (
        "G3 normalizeRecord recognizes index_progress and maps fields",
        "G3 indexProgressLabel formats path with (done/total)",
        "G3 indexProgressState flips to completed only when done>=total>0",
        "G3 index_progress renders an .index-progress-marker with status class",
        # The single-in-place-line convergence — the core rendering contract:
        # successive per-file markers of one step collapse to one climbing line,
        # the terminal marker wins, and incremental append stays a single line.
        "G3 successive records of one step converge to one in-place line",
        "G3 the terminal marker wins even if records arrive out of order",
        "G3 index_progress markers do not disturb an interleaved assistant bubble",
        "G3 markers of different steps stay independent, never folded together",
        "G3 incremental append of further records stays one in-place line",
    ):
        assert needle in combined, (
            f"expected G3 check {needle!r} in node output:\n{combined}"
        )
    assert "checks passed" in combined, combined


def test_index_progress_css_distinguishes_running_completed():
    """`.index-progress-marker` must visually distinguish running/completed.

    A cheap static guard mirroring the group-status marker check: the running
    and completed state selectors must set a distinct ``border-left-color``.
    """
    assert STYLE_CSS.is_file(), f"missing {STYLE_CSS}"
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".index-progress-marker" in css, "index-progress-marker CSS is missing"
    colors = {}
    for status in ("running", "completed"):
        marker = f".index-progress-marker.status-{status}"
        idx = css.find(marker)
        assert idx != -1, f"missing CSS rule for {marker}"
        block = css[idx : css.find("}", idx)]
        line = next(
            (ln for ln in block.splitlines() if "border-left-color" in ln),
            None,
        )
        assert line is not None, f"{marker} must set border-left-color"
        colors[status] = line.strip()
    assert len(set(colors.values())) == 2, (
        f"running/completed must use distinct colors, got {colors}"
    )
