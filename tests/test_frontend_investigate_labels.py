"""Pytest bridge for the INVESTIGATE step's WebUI labels (Group G6).

The behavioural assertions live in the standalone Node suite
``tests/frontend/investigate_step_labels.test.mjs`` (same pattern as
``tests/test_frontend_i18n.py`` bridges ``i18n_render_switch.test.mjs``); this
module runs it, asserts the key checks actually executed, and adds the static
asset guards that do not need a JS runtime — so a machine without ``node`` still
catches a missing catalog key rather than silently skipping the whole area.

The Node suite is skipped when ``node`` is not on PATH; it stays runnable by
hand via ``node tests/frontend/investigate_step_labels.test.mjs``.
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
I18N_DIR = STATIC_DIR / "i18n"
NODE_TEST = REPO_ROOT / "tests" / "frontend" / "investigate_step_labels.test.mjs"

LOCALES = ("en-US", "zh-CN")


def _catalog(code: str) -> dict:
    return json.loads((I18N_DIR / f"{code}.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. Static asset guards (no JS runtime required)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code", LOCALES)
@pytest.mark.parametrize("key", ["stepHeader.investigate", "stepReport.investigate"])
def test_investigate_step_label_keys_present(code, key):
    value = _catalog(code).get(key)
    assert isinstance(value, str) and value.strip(), f"{code} is missing {key}"


@pytest.mark.parametrize("code", LOCALES)
def test_survey_task_type_key_present_and_directive_gone(code):
    catalog = _catalog(code)
    assert catalog.get("taskType.survey"), f"{code} is missing taskType.survey"
    assert "taskType.directive" not in catalog, (
        f"{code} still carries the retired taskType.directive key"
    )


def test_app_js_label_maps_carry_investigate():
    """Both label maps need the entry: the report card and the conversation
    step header resolve through different tables, and a step type missing from
    a map never reaches its i18n lookup at all."""
    js = APP_JS.read_text(encoding="utf-8")
    for marker in (
        'investigate: "INVESTIGATE"',
        'investigate: "Root-Cause Investigation"',
    ):
        assert marker in js, f"app.js label maps are missing {marker!r}"


# ---------------------------------------------------------------------------
# 2. Node suite
# ---------------------------------------------------------------------------
def test_investigate_labels_node_module_present():
    assert NODE_TEST.is_file(), f"missing {NODE_TEST}"


def test_investigate_labels_node_suite_passes():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")
    result = subprocess.run(
        [node, str(NODE_TEST)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"investigate label runner exited {result.returncode}:\n{combined}"
    )
    for needle in (
        "stepHeaderLabel('investigate') is localized in en-US",
        "stepHeaderLabel('investigate') is localized in zh-CN",
        "stepHeaderLabel('investigate') degrades to the map literal offline",
        "reportCardTitle('investigate') is localized in zh-CN",
        "an unknown step type still falls back to the supplied label",
        "both catalogs carry taskType.survey and no retired taskType.directive",
        "an unknown task type falls through to its raw string",
    ):
        assert needle in combined, (
            f"expected check {needle!r} in node output:\n{combined}"
        )
