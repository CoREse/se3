"""Pytest bridge for the WebUI interjection-dialog surface.

The behavioural assertions live in
``tests/frontend/interjection_dialog.test.mjs``, driven by the Node harness
``tests/frontend/test_app_pure.mjs``. This module pulls that suite into the
pytest run and adds the static guardrails that belong on the shipped assets:
the ``dialog`` call kind being registered, and i18n key parity across both
locale bundles (a key added to one and forgotten in the other would paint
English into a zh-CN console).
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
SUITE = REPO_ROOT / "tests" / "frontend" / "interjection_dialog.test.mjs"

DIALOG_I18N_KEYS = [
    "intervention.dialog.label",
    "intervention.dialog.hint",
    "dialog.speakerUser",
    "dialog.speakerAgent",
    "dialog.sameSession",
    "dialog.action.continue",
    "dialog.action.restart",
    "dialog.action.exit",
    "dialog.workspace.keep",
    "dialog.workspace.reset",
    "dialog.field.action",
    "dialog.field.restartStep",
    "dialog.field.workspace",
    "dialog.field.instruction",
    "dialog.field.revised",
    "dialog.currentStep",
    "dialog.confirm",
    "dialog.applied",
    "dialog.appliedEcho",
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_node_suite_runs_the_dialog_checks():
    result = subprocess.run(
        ["node", str(HARNESS)],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # Assert the dialog checks actually executed — a silently unregistered
    # suite would otherwise leave this test green while testing nothing.
    assert "dialog record keeps its kind through normalizeRecord" in result.stdout
    assert "a proposed decision renders every field as an editable control" in result.stdout


def test_dialog_suite_is_registered_with_the_harness():
    assert SUITE.exists()
    assert "interjection_dialog.test.mjs" in HARNESS.read_text(encoding="utf-8")


def test_app_js_registers_the_dialog_call_kind():
    source = APP_JS.read_text(encoding="utf-8", errors="surrogateescape")
    assert "  dialog: {" in source
    assert "renderDialogPanel" in source
    assert "sendDialogDecision" in source
    assert "renderDialogTurnRecord" in source


def test_dialog_decision_is_sent_as_a_structured_payload():
    """A free-text reply is the "keep talking" channel; the decision travels
    as ``{decision: {...}}`` so the two can never be confused."""
    source = APP_JS.read_text(encoding="utf-8", errors="surrogateescape")
    assert "const payload = { decision: decision };" in source
    assert "response: payload," in source


def test_an_edited_reset_asks_for_a_preview_before_it_can_be_applied():
    """The fields are editable, so the confirmed decision is not necessarily the
    one the published preview describes. Turning a proposal into
    restart+reset must first fetch a preview instead of discarding the tree."""
    source = APP_JS.read_text(encoding="utf-8", errors="surrogateescape")
    assert "const previewUsable = " in source
    assert 'edited.action === "restart" && edited.workspace === "reset"' in source
    assert "payload.preview_request = true" in source


@pytest.mark.parametrize("key", DIALOG_I18N_KEYS)
def test_dialog_i18n_keys_exist_in_both_bundles(key):
    en = json.loads(EN_JSON.read_text(encoding="utf-8"))
    zh = json.loads(ZH_JSON.read_text(encoding="utf-8"))
    assert key in en, f"{key} missing from en-US"
    assert key in zh, f"{key} missing from zh-CN"
    assert en[key].strip()
    assert zh[key].strip()


def test_dialog_styles_are_shipped():
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    for selector in (
        ".flow-reply-dialog",
        ".flow-reply-dialog-transcript",
        ".flow-reply-dialog-decision",
        ".conv-record.kind-dialog",
    ):
        assert selector in css, f"{selector} missing from style.css"
