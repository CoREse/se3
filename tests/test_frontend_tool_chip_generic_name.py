"""Pytest bridge for the whitelist-free structured tool-chip name parsing.

A tool call renders in the web console as one chip driven by two
``stream_progress`` fragments — an in-flight one on ``tool_use`` and a terminal
one on ``tool_result``. The frontend used to read the chip's tool name out of
the fragment via the ``TOOL_MARKER_NAMES`` whitelist, which covered only a
handful of built-ins. Every other tool (claude's ``Agent`` / ``ReportFindings``
/ ``ToolSearch`` / ``Skill``, codex's synthesized ``mcp__<server>__<tool>`` and
``unknown``) matched nothing on its terminal fragment, so upgrading the chip
rebuilt its head with an EMPTY header and the completed call showed only
"Tool ✓".

The behavioural assertions live in
``tests/frontend/tool_chip_generic_name.test.mjs``, which the Node assertion
harness ``tests/frontend/test_app_pure.mjs`` loads and runs. This module pulls
that suite into the pytest run, asserts the checks actually executed, and adds
static-source guardrails that the two halves of the fix are still in place:
the structured path no longer consults the whitelist, while the legacy
prose-slicing path still does.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_JS = REPO_ROOT / "src" / "tianluo" / "server" / "static" / "app.js"
FRONTEND_TEST = REPO_ROOT / "tests" / "frontend" / "test_app_pure.mjs"
CHIP_NAME_TEST = REPO_ROOT / "tests" / "frontend" / "tool_chip_generic_name.test.mjs"


def test_chip_generic_name_module_present():
    """The registrable mjs module exists and is wired into the harness."""
    assert CHIP_NAME_TEST.is_file(), f"missing {CHIP_NAME_TEST}"
    harness = FRONTEND_TEST.read_text(encoding="utf-8")
    assert "tool_chip_generic_name.test.mjs" in harness, (
        "tool_chip_generic_name.test.mjs is not registered in test_app_pure.mjs"
    )
    assert "registerToolChipGenericNameTests" in harness


def test_structured_path_parses_the_name_generically():
    """`applyFragmentToBubble` must not fall back to the name whitelist.

    The whitelist stays alive for `renderToolMarkers` (prose slicing), so its
    definition must still exist — but the structured path must reach for
    `parseToolFragmentName` instead.
    """
    src = APP_JS.read_text(encoding="utf-8")
    assert "function parseToolFragmentName(" in src
    assert "const TOOL_MARKER_NAMES = [" in src, (
        "the legacy prose-slicing whitelist must survive — dropping it would "
        "turn Markdown links into tool chips"
    )
    start = src.index("function applyFragmentToBubble(")
    end = src.index("function appendPartialFragment(", start)
    body = src[start:end]
    assert "parseToolFragmentName(" in body, (
        "the structured chip path must read the tool name generically"
    )
    assert "TOOL_MARKER_RE" not in body, (
        "the structured chip path must no longer match against the whitelist"
    )
    # renderToolMarkers — the legacy path — still uses it.
    legacy_start = src.index("function renderToolMarkers(")
    legacy_end = src.index("\n}", legacy_start)
    assert "TOOL_MARKER_RE" in src[legacy_start:legacy_end]


def test_upgrade_helpers_preserve_a_non_empty_header():
    """An empty terminal header must never blank an existing chip header."""
    src = APP_JS.read_text(encoding="utf-8")
    assert "function _preserveChipHeader(" in src
    for fn in ("function upgradeChipToSuccess(", "function upgradeChipToFailure("):
        start = src.index(fn)
        end = src.index("\n}", start)
        assert "_preserveChipHeader(chip, header)" in src[start:end], (
            f"{fn} must route its header through the empty-header guard"
        )


def test_frontend_chip_generic_name_node_suite_passes():
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
        "(A1) Agent in-flight→success keeps the Agent name and a non-empty header",
        "(A2) codex mcp__server__tool name is parsed whole and upgrades correctly",
        "(A2b) codex 'unknown' tool name renders as its own chip name",
        "(A3) an empty terminal header never blanks the in-flight header",
        "(A3b) upgradeChipToSuccess called directly with '' preserves the header",
        "(A3c) upgradeChipToFailure called directly with '' preserves the header",
        "(A4) legacy '[Tool: Agent | Input: …]' + '[Agent ✓ …]' still upgrades to Agent",
        "(A4b) legacy in-flight alone still renders as a Tool-named chip",
        "(A5) '[Tool error: …]' keeps name=Tool and header='error: …'",
        "(A6) renderToolMarkers still ignores Markdown links in prose",
        "(A6b) renderToolMarkers still slices a whitelisted inline marker",
        "(A6c) renderToolMarkers leaves a non-whitelisted bracket as prose",
        "(A7) parseToolFragmentName reads the leading token, or null",
        "(A7b) parseToolBracket on a generic name yields a clean header",
    ):
        assert f"ok - {needle}" in combined, (
            f"expected check did not run: {needle}\n{combined[-4000:]}"
        )
