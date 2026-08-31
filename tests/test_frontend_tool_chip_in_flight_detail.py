"""Pytest bridge for the in-flight tool-chip detail panel.

A tool call renders in the web console as one chip driven by two
``stream_progress`` fragments. Only the terminal one used to carry a
``tool_detail`` payload, so while a call was running its chip showed a 60-char
header and could not be opened at all — a Bash ``command`` or an Agent
``prompt`` was simply unreadable until the call finished. The backend now
attaches a ``kind="tool_input"`` payload on ``tool_use`` as well, and an
unregistered tool's settled payload carries the call's ``input`` alongside its
result text.

The behavioural assertions live in
``tests/frontend/tool_chip_in_flight_detail.test.mjs``, which the Node
assertion harness ``tests/frontend/test_app_pure.mjs`` loads and runs. This
module pulls that suite into the pytest run, asserts the checks actually
executed, and adds static guardrails on the pieces a pure-DOM test cannot see:
the new renderer registration, the i18n keys behind its labels, its CSS, and —
most importantly — that nothing infers "still in flight" from an absent
``tool_detail`` now that in-flight records carry one.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC = REPO_ROOT / "src" / "tianluo" / "server" / "static"
APP_JS = STATIC / "app.js"
STYLE_CSS = STATIC / "style.css"
I18N = STATIC / "i18n"
FRONTEND_TEST = REPO_ROOT / "tests" / "frontend" / "test_app_pure.mjs"
DETAIL_TEST = REPO_ROOT / "tests" / "frontend" / "tool_chip_in_flight_detail.test.mjs"


def test_in_flight_detail_module_present():
    """The registrable mjs module exists and is wired into the harness."""
    assert DETAIL_TEST.is_file(), f"missing {DETAIL_TEST}"
    harness = FRONTEND_TEST.read_text(encoding="utf-8")
    assert "tool_chip_in_flight_detail.test.mjs" in harness, (
        "tool_chip_in_flight_detail.test.mjs is not registered in test_app_pure.mjs"
    )
    assert "registerToolChipInFlightDetailTests" in harness


def test_tool_input_renderer_is_registered():
    src = APP_JS.read_text(encoding="utf-8")
    assert 'registerToolDetailRenderer("tool_input"' in src
    assert "function renderToolInputBlock(" in src


def test_in_flight_branch_attaches_the_panel():
    """`applyFragmentToBubble`'s in-flight branch must mount the detail panel."""
    src = APP_JS.read_text(encoding="utf-8")
    start = src.index("function applyFragmentToBubble(")
    end = src.index("function appendPartialFragment(", start)
    body = src[start:end]
    # The payload now goes through `chipDetailForFragment`, which prefers the
    # record's inline `tool_detail` and falls back to the lazy ref the server
    # leaves when it holds a successful call's body back. Either way a running
    # call must still mount a panel.
    assert "chipDetailForFragment(norm)" in body, (
        "a running call must be expandable, not just the settled one"
    )
    assert "attachChipDetail(chip, detail" in body
    assert "/*expanded=*/false" in body, "the in-flight panel starts folded"
    src_fn = src[src.index("function chipDetailForFragment("):]
    assert "norm.toolDetail" in src_fn[: src_fn.index("\n}")], (
        "chipDetailForFragment must still prefer an inline tool_detail"
    )


def test_nothing_infers_in_flight_from_a_missing_detail():
    """INVARIANT: `is_error` alone separates in-flight from settled.

    In-flight records now carry a `tool_detail`, so any surviving
    `tool_detail is None` / `!norm.toolDetail` state test would misclassify
    every running tool call.
    """
    js = APP_JS.read_text(encoding="utf-8")
    for forbidden in (
        "toolDetail === null ?",
        "norm.toolDetail == null ?",
        "!norm.toolDetail ?",
    ):
        assert forbidden not in js, (
            f"the frontend must not branch chip state on {forbidden!r}"
        )
    py = (REPO_ROOT / "src" / "tianluo" / "engine" / "llm_caller.py").read_text(
        encoding="utf-8"
    )
    assert "tool_detail=None" not in py, (
        "the tool_use emit must carry a tool_input payload, not None"
    )


def test_i18n_keys_present_in_both_bundles():
    """New user-facing labels are rendered through `tf()` with real keys."""
    src = APP_JS.read_text(encoding="utf-8")
    assert 'tf("tool.detail.input"' in src
    assert 'tf("tool.detail.noInput"' in src
    for name in ("en-US.json", "zh-CN.json"):
        data = json.loads((I18N / name).read_text(encoding="utf-8"))
        for key in ("tool.detail.input", "tool.detail.noInput"):
            assert key in data, f"{key} missing from {name}"
            assert data[key].strip(), f"{key} is empty in {name}"


def test_input_block_styles_follow_the_tool_marker_naming():
    css = STYLE_CSS.read_text(encoding="utf-8")
    for cls in (
        ".tool-marker-input",
        ".tool-marker-input-label",
        ".tool-marker-input-row",
        ".tool-marker-input-key",
        ".tool-marker-input-pre",
    ):
        assert cls in css, f"{cls} has no style rule"


def test_frontend_in_flight_detail_node_suite_passes():
    """Run the Node assertion suite and confirm the new checks ran.

    Skipped if ``node`` is not available on PATH; the suite is still runnable
    by hand via ``node tests/frontend/test_app_pure.mjs``.
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
        "(B1) an in-flight chip carrying tool_detail gets a folded detail panel",
        "(B1b) the in-flight panel actually holds the full input",
        "(B1c) an in-flight chip WITHOUT tool_detail stays panel-free (legacy jsonl)",
        "(B1d) clicking the toggle expands the in-flight panel",
        "(B2) upgrading an in-flight chip leaves exactly one toggle and one panel",
        "(B3) tool_input renderer draws Bash as a `$ command` line",
        "(B3b) tool_input renderer draws a generic tool as a key/value list",
        "(B3c) long / multi-line values go into a <pre>",
        "(B3d) a truncated tool_input payload shows the truncation notice",
        "(B3e) an empty tool_input payload still renders without throwing",
        "(B4) text renderer shows the input block before the result",
        "(B4b) a legacy text payload with no input renders exactly as before",
        "(B4c) an empty input dict on a text payload adds no block",
        "(B5) extractAssistantChipEvents gives an unsettled tool_use a detail",
        "(B5b) renderChipEvents attaches the in-flight detail panel",
        "(B5c) a huge string in a raw_json tool_use is cut at the shared cap",
        "(B6) an unregistered tool's terminal detail carries `input`",
        "(B6b) a registered tool's terminal detail keeps its own shape",
        "(B7) a tool_detail-bearing record is in-flight iff is_error is absent",
    ):
        assert f"ok - {needle}" in combined, (
            f"expected check did not run: {needle}\n{combined[-4000:]}"
        )
