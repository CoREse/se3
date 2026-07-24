"""Pytest bridge for the WebUI resume-flow pure helpers.

``tests/frontend/flow_resume.test.mjs`` is a standalone Node assertion suite
(same pattern as ``tests/frontend/end_session.test.mjs``) covering
``isFlowResumable`` / ``isResumeInProgress`` and — since the shared-filesystem
machine-switch fix — ``resumeErrorText``, which decides the toast wording for a
404 resume rejection.

WHY this bridge exists: the mjs suite previously had no pytest entry point, so
its assertions never ran in ``pytest tests/``. The 404 branch depends on a weak
contract (substring-matching the backend's English ``detail``), which is
exactly the kind of thing that must be re-checked on every run rather than by
hand. This module pulls the suite into the pytest run and adds the static
guards that the helper is exported and the new locale key is shipped in both
catalogs.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "src" / "se3" / "server" / "static"
APP_JS = STATIC_DIR / "app.js"
I18N_DIR = STATIC_DIR / "i18n"
FLOW_RESUME_TEST = REPO_ROOT / "tests" / "frontend" / "flow_resume.test.mjs"


# ---------------------------------------------------------------------------
# 1. Node suite — the pure helpers actually run and pass
# ---------------------------------------------------------------------------
def test_flow_resume_module_present():
    assert FLOW_RESUME_TEST.is_file(), f"missing {FLOW_RESUME_TEST}"


def test_frontend_flow_resume_node_suite_passes():
    """Run the Node assertion suite and confirm the resume checks ran.

    Skipped if ``node`` is not available on PATH; the suite is still runnable
    by hand via ``node tests/frontend/flow_resume.test.mjs``.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")
    result = subprocess.run(
        [node, str(FLOW_RESUME_TEST)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"flow-resume test runner exited {result.returncode}:\n{combined}"
    )
    for needle in (
        "failed flow with flow_id is resumable",
        "completed flow is never resumable even with resumable=true flag",
        "isResumeInProgress returns true when flow is in the set",
        # The 404-detail decision matrix (shared-FS machine switch).
        "404 with an offline-machine detail is not the generic not-found text",
        "404 with a flow-not-found detail is passed through verbatim",
        "404 without a detail falls back to the default not-found text",
        "non-404 statuses pass the detail through unchanged",
    ):
        assert needle in combined, (
            f"expected flow-resume check {needle!r} in node output:\n{combined}"
        )
    assert "checks passed" in combined, combined


# ---------------------------------------------------------------------------
# 2. app.js static guards
# ---------------------------------------------------------------------------
def test_app_js_exports_resume_error_text():
    js = APP_JS.read_text(encoding="utf-8")
    assert "function resumeErrorText(" in js
    assert "\n    resumeErrorText,\n" in js, (
        "resumeErrorText must be exposed via module.exports for the node suite"
    )


def test_resume_404_branch_reads_the_backend_detail():
    """The 404 branch must consult the backend detail, not blanket-toast."""
    js = APP_JS.read_text(encoding="utf-8")
    assert "resumeErrorText(404, detail)" in js


# ---------------------------------------------------------------------------
# 3. Locale catalogs carry the machine-offline wording
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("lang", ["en-US", "zh-CN"])
def test_machine_offline_key_shipped(lang):
    data = json.loads((I18N_DIR / f"{lang}.json").read_text(encoding="utf-8"))
    value = data.get("toast.resumeMachineOffline")
    assert isinstance(value, str) and value.strip(), (
        f"{lang} is missing a non-empty toast.resumeMachineOffline"
    )
