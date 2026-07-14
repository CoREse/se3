"""Pytest bridge for the web console's step_type token-safety fix (Bug A).

``appendLocalReply`` used to stamp the optimistic echo's ``step_type`` with a
RENDERED i18n label: under zh-CN that is "待回复 回复", which contains a space.
But ``step_type`` is an IDENTIFIER — it is used as a DOM class suffix
(``step-type-<type>``), as a grouping key, and as the step-header fallback — so
``tagStepType``'s ``classList.add()`` threw ``InvalidCharacterError`` on it. The
echo then lived on in ``state.flowConversationRecords``, so every subsequent
``applyHistoryData → renderConversation → addConversationRecords`` threw again on
the un-guarded ``ws.onmessage`` path: the whole chat view froze until the reader
exited and re-entered the session.

The fix has two layers: the echo now carries a machine-safe ``reply_<kind>``
token (its localized header text is resolved separately at render time), and the
renderer is defensive regardless of who wrote the record (``tagStepType``
sanitizes / skips, and each record's post-render bookkeeping is isolated so one
dirty record cannot take the whole batch down).

The DOM-stub behavioral assertions live in
``tests/frontend/step_type_token_safety.test.mjs``, which the Node assertion
harness ``tests/frontend/test_app_pure.mjs`` loads and runs. This module pulls
that suite into the pytest run, asserts the checks actually executed, and adds
static-source guardrails on ``app.js``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "src" / "se3" / "server" / "static"
APP_JS = STATIC_DIR / "app.js"
FRONTEND_TEST = REPO_ROOT / "tests" / "frontend" / "test_app_pure.mjs"
TOKEN_TEST = REPO_ROOT / "tests" / "frontend" / "step_type_token_safety.test.mjs"


def test_step_type_token_safety_module_present():
    """The mjs module exists and is registered into the Node harness."""
    assert TOKEN_TEST.is_file(), f"missing {TOKEN_TEST}"
    harness = FRONTEND_TEST.read_text(encoding="utf-8")
    assert "step_type_token_safety.test.mjs" in harness, (
        "step_type_token_safety.test.mjs is not registered in test_app_pure.mjs"
    )
    assert "registerStepTypeTokenSafetyTests" in harness


def test_frontend_step_type_token_safety_node_suite_passes():
    """Run the Node assertion suite and confirm the token-safety checks ran.

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
        timeout=120,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"frontend test runner exited {result.returncode}:\n{combined}"
    )
    for needle in (
        "sanitizeDomToken folds whitespace and drops illegal characters",
        "tagStepType never throws and never adds an illegal class",
        "a space-bearing step_type record renders and does not stall the batch",
        "a record whose step_type throws on coercion loses only its own bubble",
        "an incremental append past a dirty record keeps flowing",
        "zh-CN appendLocalReply writes a legal DOM token, not an i18n label",
        "the echo is still reconciled away by its authoritative record",
    ):
        assert needle in combined, (
            f"expected token-safety check {needle!r} in node output:\n{combined}"
        )
    assert "checks passed" in combined, combined


def _append_local_reply_body() -> str:
    """The source text of ``appendLocalReply``, up to the next top-level function."""
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index("function appendLocalReply(")
    end = js.index("\nfunction ", start + 1)
    return js[start:end]


def test_local_echo_step_type_is_not_an_i18n_render():
    """The echo's ``step_type`` must never be produced by ``tf()`` / I18N.

    This is the root cause: a rendered label is a *human* string (zh-CN's
    "待回复 回复" carries a space), and putting it in an identifier field made
    the DOM's validity depend on the active language pack.
    """
    body = _append_local_reply_body()
    step_type_assign = re.search(r"step_type:\s*([^\n]*)", body)
    assert step_type_assign, f"appendLocalReply no longer sets step_type:\n{body}"
    assigned = step_type_assign.group(1)
    assert "tf(" not in assigned and "I18N" not in assigned, (
        "appendLocalReply's step_type must be a machine-safe token, never an "
        f"i18n render: {assigned!r}"
    )
    assert "replyStepType(" in assigned, (
        f"appendLocalReply's step_type must come from replyStepType(): {assigned!r}"
    )


def test_reply_step_type_token_is_ascii_and_language_independent():
    """``replyStepType`` composes an ASCII token from the canonical kind alone."""
    js = APP_JS.read_text(encoding="utf-8")
    assert 'const REPLY_STEP_TYPE_PREFIX = "reply_";' in js
    match = re.search(
        r"function replyStepType\(kind\) \{\n(.*?)\n\}", js, re.DOTALL)
    assert match, "replyStepType() helper must exist"
    assert "normalizeKind(kind)" in match.group(1), (
        "the token must be derived from the canonical (ASCII) kind"
    )
    assert "tf(" not in match.group(1) and "I18N" not in match.group(1)


def test_tag_step_type_sanitizes_before_touching_classlist():
    """``tagStepType`` must sanitize, never hand a raw step_type to classList."""
    js = APP_JS.read_text(encoding="utf-8")
    assert "function sanitizeDomToken(" in js, (
        "a DOM-token sanitizer must exist"
    )
    match = re.search(r"function tagStepType\(bubble, stepType\) \{\n(.*?)\n\}",
                      js, re.DOTALL)
    assert match, "tagStepType() must exist"
    body = match.group(1)
    assert "sanitizeDomToken(stepType)" in body, (
        "tagStepType must sanitize the step type before adding a class"
    )
    assert 'classList.add("step-type-" + key)' in body


def test_conversation_record_rendering_is_isolated_per_record():
    """One dirty record must not break the whole conversation render.

    The per-record bookkeeping that follows the bubble build (ordering metadata,
    step-type class, supersede tags, insert) all reads untrusted record fields
    and sits on the ws.onmessage → applyHistoryData → renderConversation path, so
    it must be isolated too — not just the bubble build.
    """
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index("function addConversationRecords(")
    end = js.index("\nfunction ", start + 1)
    body = js[start:end]
    assert '"conversation record post-render failed"' in body, (
        "the post-render bookkeeping must be wrapped in its own try/catch that "
        "logs and drops only the offending record"
    )
    assert "WHY:" in body, (
        "the one-dirty-record-must-not-break-the-render invariant must be "
        "recorded as a WHY: comment"
    )
