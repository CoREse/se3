"""Pytest-side checks for the web console's running-flow rendering rules.

Most of the running-flow rendering logic lives in DOM-free helpers inside
``src/se3/server/static/app.js``; the deeper behavioural assertions for those
helpers live in ``tests/frontend/test_app_pure.mjs`` (a Node assertion
suite). This pytest module pulls those checks into the pytest run as well,
and supplements them with three static-source guardrails that codify the
running-flow-console spec contracts directly against the JS / CSS bytes:

1. ``KIND_META`` chip labels MUST NOT leak the internal transport vocabulary
   (``MCP`` / ``call_id`` / ``call <hex-id>``) as visible text.
2. The conversation-range code-block CSS selectors MUST wrap long lines via
   ``white-space: pre-wrap`` + a per-character break rule, with no inner
   horizontal scrollbar — the Long-Content Wrapping requirement.
3. Every step prompt template MUST inject the ``TEMPLATE_PREFIX_END`` /
   ``USER_CONTENT_BEGIN`` marker pair so the running-flow console can split
   the user message into a default-collapsed system-prompt chip and a
   default-expanded user bubble — the Role-Based Message Collapse
   requirement.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = REPO_ROOT / "src" / "se3" / "server" / "static"
APP_JS = STATIC_DIR / "app.js"
STYLE_CSS = STATIC_DIR / "style.css"
FRONTEND_TEST = REPO_ROOT / "tests" / "frontend" / "test_app_pure.mjs"


# ---------------------------------------------------------------------------
# 1. Bridge: run the Node-side frontend suite from pytest when node is on PATH
# ---------------------------------------------------------------------------


def test_frontend_node_assertion_suite_passes():
    """Run the Node assertion suite covering extractAssistantText shapes,
    KIND_META neutrality, splitUserPromptByMarker, normalizeRecord, and the
    step-report renderer registry.

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
        timeout=60,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"frontend test runner exited {result.returncode}:\n{combined}"
    )
    # Sanity: the script always prints a trailing "N checks passed." summary.
    assert "checks passed" in combined, combined


# ---------------------------------------------------------------------------
# 2. Static guardrail: KIND_META visible labels carry no implementation jargon
# ---------------------------------------------------------------------------


def _read_app_js() -> str:
    assert APP_JS.is_file(), f"missing {APP_JS}"
    return APP_JS.read_text(encoding="utf-8")


def _extract_js_function_body(src: str, name: str) -> str:
    """Return the brace-balanced body of ``function <name>(...) { ... }``.

    A small, dependency-free brace matcher: locate the ``function <name>``
    declaration, find its first ``{`` and scan forward counting braces until
    the matching close. Good enough for the static-source guardrails below,
    which only need to screen a single function's text for the presence /
    absence of a few call expressions.
    """
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
    assert m, f"could not locate function {name!r} in app.js"
    open_idx = src.index("{", m.end())
    depth = 0
    for i in range(open_idx, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[open_idx : i + 1]
    raise AssertionError(f"unbalanced braces while scanning function {name!r}")


def _extract_kind_meta_block(src: str) -> str:
    """Return the literal text of the ``const KIND_META = { … };`` block.

    The block is matched as the slice from ``const KIND_META = {`` up to (and
    including) the closing ``};`` line. The web view's chip-bar visible
    strings live exclusively here, so static screening this block is a
    sufficient guard against MCP / call_id leakage in the four labels.
    """
    m = re.search(
        r"const\s+KIND_META\s*=\s*\{.*?^\};\s*$",
        src,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert m, "could not locate KIND_META block in app.js"
    return m.group(0)


def test_kind_meta_block_contains_no_mcp_or_call_id_literals():
    """The four chip labels are user-facing text; they must not contain the
    internal transport vocabulary."""
    block = _extract_kind_meta_block(_read_app_js())
    # Case-insensitive: catches both "MCP" and "Mcp" / "call_id" / "Call_id".
    assert not re.search(r"\bMCP\b", block, flags=re.IGNORECASE), (
        "KIND_META block must not contain the 'MCP' literal as visible text"
    )
    assert "call_id" not in block.lower(), (
        "KIND_META block must not surface 'call_id' as visible text"
    )


def test_no_visible_call_id_template_strings_in_chip_or_reply_header():
    """The chip label and reply-header builders must not embed a visible
    ``call <id>`` template string. The call_id is still kept on hidden DOM
    attributes (``data-call-id``) and tooltips for debugging.
    """
    src = _read_app_js()
    # `call ${...}` followed by ${entry.callId} or similar is the historical
    # offender; a literal `"call " + ` concatenation likewise leaks it.
    leak_patterns = [
        re.compile(r'["`]\s*call\s+\$\{[^}]*call', re.IGNORECASE),
        re.compile(r'["`]\s*call\s*["`]\s*\+\s*[a-zA-Z_]*call', re.IGNORECASE),
    ]
    for pat in leak_patterns:
        match = pat.search(src)
        assert match is None, (
            f"visible call_id leak pattern detected in app.js: {match.group(0)!r}"
        )


# ---------------------------------------------------------------------------
# 2b. Static guardrail: message-paradigm rendering placement
# ---------------------------------------------------------------------------
#
# These codify the message paradigm (B1) the running-flow chat must match:
#   * "查看原始" (raw toggle) is never a row-level always-visible control — on the
#     user side it is nested inside the "展开全部" expand area / a chip's expand
#     detail; on the assistant side it is the single fold below the rendered
#     result (makeAssistantRawToggle).
#   * an assistant turn with NO result JSON shows its thinking process inline
#     (renderToolMarkers), never folded/contracted via makeFoldable.
#   * conversation step headers use the paradigm step names via STEP_HEADER_TITLES.


def test_raw_toggle_is_never_appended_at_row_level():
    """The "查看原始" toggle must not be a row-level always-visible control.

    For the USER side the raw toggle is appended into an expand area — the
    ``makeUserPromptToggle`` "展开全部" body, or a collapsed chip's expand detail.
    For the ASSISTANT side it is the single fold built below the rendered result
    by ``makeAssistantRawToggle``. Either way the historical row-level form
    (``row.appendChild(rawToggle)``) must be gone, so the default view never
    shows a row-level raw toggle.
    """
    src = _read_app_js()
    assert "row.appendChild(rawToggle)" not in src, (
        "查看原始 must not be appended at the row level — nest it inside the "
        "展开全部 expand area / chip detail (user) or build it below the result "
        "via makeAssistantRawToggle (assistant) instead"
    )


def test_raw_toggle_is_nested_inside_expand_area_factories():
    """``makeUserPromptToggle`` MUST nest the user-side Layer-3 raw toggle inside
    its lazily-built "展开全部" expand area (user Layer 3 inside Layer 2).

    The user side uses the dedicated ``makeUserRawToggle`` (永不返回 null — falls
    back to the .jsonl envelope when no second-layer raw payload exists) so a
    user turn's Layer 3 is always reachable, even when raw_json=[] and
    raw_ndjson is absent (the regression-A fix). The assistant side has no
    ``makeProcessToggle`` — it uses the single ``makeAssistantRawToggle`` fold —
    so only the user factory is asserted here.
    """
    src = _read_app_js()
    body = _extract_js_function_body(src, "makeUserPromptToggle")
    assert "makeUserRawToggle" in body, (
        "makeUserPromptToggle must nest makeUserRawToggle inside its 展开全部 "
        "expand area so Layer 3 is always reachable"
    )


def test_user_layer3_raw_toggle_not_gated_on_raw_payload():
    """Regression A: the user turn's Layer 3 ("查看原始") must be stably reachable
    regardless of whether a second-layer raw payload exists.

    Concretely the hasContent branch of ``renderUserMarkerRecord`` must
    unconditionally provide the "展开全部" toggle (which nests Layer 3) — it must
    NOT gate it on ``hasRawPayload(norm)`` / ``hasPrefix`` / ``hasSuffix`` the
    way the buggy version did, because a user record carries raw_json=[] and no
    raw_ndjson, so a hasRawPayload gate would have suppressed Layer 3 entirely.
    """
    body = _extract_js_function_body(_read_app_js(), "renderUserMarkerRecord")
    assert "makeUserPromptToggle(split, norm)" in body, (
        "renderUserMarkerRecord must build the Layer-2/3 user-prompt toggle"
    )
    # The toggle must be provided unconditionally — no hasRawPayload gate.
    assert "hasRawPayload" not in body, (
        "the user Layer-2/3 toggle must not be gated on hasRawPayload — the "
        "user side reaches its original .jsonl envelope via makeUserRawToggle, "
        "which never returns null"
    )


def test_make_raw_toggle_null_contract_is_preserved():
    """The shared ``makeRawToggle`` MUST keep its "无 raw 载荷 → null" contract.

    Regression A is fixed via a *separate* user-side helper
    (``makeUserRawToggle``); the shared helper's null contract — relied on by
    other call sites — must NOT be weakened. So ``makeRawToggle`` must still
    early-return ``null`` when ``resolveRawPayload`` yields no payload.
    """
    src = _read_app_js()
    body = _extract_js_function_body(src, "makeRawToggle")
    assert "return null" in body, (
        "makeRawToggle must preserve its 'no raw payload → null' contract"
    )
    # And the dedicated user helper must exist (永不返回 null path).
    user_body = _extract_js_function_body(src, "makeUserRawToggle")
    assert "envelope" in user_body, (
        "makeUserRawToggle must fall back to the .jsonl envelope record"
    )
    assert "return null" not in user_body, (
        "makeUserRawToggle must never return null — Layer 3 is always reachable"
    )


def test_assistant_no_result_branch_does_not_fold():
    """``renderAssistantBubble`` MUST NOT route any branch through
    ``makeFoldable``: the no-result turn shows its thinking inline, and the
    result-JSON turn keeps the narrative + result visible while folding only the
    original record behind the single ``makeAssistantRawToggle`` "查看原始" entry
    — neither collapses the thinking into a fold.

    The assistant side is now two layers (no ``makeProcessToggle`` / "展开全部"
    wrapper), so the result branch must call the dedicated single raw entry."""
    body = _extract_js_function_body(_read_app_js(), "renderAssistantBubble")
    assert "makeFoldable" not in body, (
        "renderAssistantBubble must not fold the thinking process — a no-result "
        "assistant turn shows it inline via renderToolMarkers"
    )
    # The assistant two-layer model: the result branch builds the single
    # 查看原始 fold via makeAssistantRawToggle, not a 展开全部 process toggle.
    assert "makeAssistantRawToggle" in body, (
        "renderAssistantBubble must build the single 查看原始 fold for the "
        "result-JSON branch via makeAssistantRawToggle"
    )
    assert "makeProcessToggle" not in body, (
        "renderAssistantBubble must not reference the deleted makeProcessToggle "
        "— the assistant side is now two layers"
    )


def test_step_header_titles_map_exists_and_is_used():
    """A paradigm step-name map (STEP_HEADER_TITLES) MUST exist with the
    paradigm headings, and ``rebuildStepHeaders`` MUST resolve labels through
    ``stepHeaderLabel`` rather than emitting the raw step_type literal."""
    src = _read_app_js()
    assert "const STEP_HEADER_TITLES" in src, (
        "missing STEP_HEADER_TITLES map for conversation step headers"
    )
    m = re.search(
        r"const\s+STEP_HEADER_TITLES\s*=\s*\{.*?^\};\s*$",
        src,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert m, "could not locate STEP_HEADER_TITLES block in app.js"
    block = m.group(0)
    for heading in [
        "DISCOVERY", "ANALYZE", "PLAN", "IMPLEMENT", "TEST", "SELF CHECK",
        "UPDATE SPEC", "VERSION ANALYZE", "COMMIT", "SUMMARY",
    ]:
        assert heading in block, (
            f"STEP_HEADER_TITLES must include the paradigm heading {heading!r}"
        )
    body = _extract_js_function_body(src, "rebuildStepHeaders")
    assert "stepHeaderLabel" in body, (
        "rebuildStepHeaders must resolve step names via stepHeaderLabel"
    )


# ---------------------------------------------------------------------------
# 3. Static guardrail: long-line wrapping CSS rules
# ---------------------------------------------------------------------------


def _read_style_css() -> str:
    assert STYLE_CSS.is_file(), f"missing {STYLE_CSS}"
    return STYLE_CSS.read_text(encoding="utf-8")


def _extract_rule_body(css: str, selector: str) -> str:
    """Return the body of the CSS rule whose selector matches *selector*.

    Selectors with dots / spaces are matched as a literal prefix on a line,
    followed by the opening ``{`` (possibly after whitespace). The body is
    everything between the matching braces, exclusive.
    """
    pattern = re.compile(
        r"^" + re.escape(selector) + r"\s*\{([^}]*)\}",
        flags=re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(css)
    assert m, f"could not locate CSS rule for selector {selector!r}"
    return m.group(1)


@pytest.mark.parametrize(
    "selector",
    [
        ".conv-bubble .md-code",
        ".raw-json",
        ".step-report__markdown .md-code",
    ],
)
def test_conversation_code_block_wraps_long_lines(selector: str):
    """The conversation-range code-block selectors must wrap long lines.

    Concretely we require ``white-space: pre-wrap`` plus a break rule
    (``overflow-wrap: anywhere`` or ``word-break: break-word``), and we
    forbid ``overflow-x: auto`` which would produce an inner horizontal
    scrollbar inside the chat bubble — exactly the regression spelled out by
    the Long-Content Wrapping requirement.
    """
    body = _extract_rule_body(_read_style_css(), selector)
    assert "white-space: pre-wrap" in body, (
        f"{selector} must use 'white-space: pre-wrap' to wrap long single-line "
        f"payloads (got: {body!r})"
    )
    assert (
        "overflow-wrap: anywhere" in body
        or "word-break: break-word" in body
    ), (
        f"{selector} must declare a per-character break rule "
        f"(overflow-wrap: anywhere / word-break: break-word)"
    )
    # No inner horizontal scrollbar: explicit `overflow-x: auto` is the bug
    # we are guarding against. `overflow-x: hidden` (or the property being
    # absent) is fine.
    assert "overflow-x: auto" not in body, (
        f"{selector} must NOT use 'overflow-x: auto'; long single lines should "
        f"wrap rather than open an inner horizontal scrollbar"
    )


# ---------------------------------------------------------------------------
# 3b. Static guardrail: tool-call chip folded state collapses the whole wrapper
# ---------------------------------------------------------------------------


def test_tool_marker_details_folded_collapses_whole_wrapper():
    """Regression B: the folded tool-call chip detail must collapse the ENTIRE
    ``.tool-marker-details`` wrapper, not merely the inner
    ``.tool-marker-details-body``.

    The wrapper carries ``flex-basis:100%`` + ``margin-top:4px``; in a
    flex-wrap chip, hiding only the body left the wrapper claiming a full empty
    row below the collapsed chip (the stray blank band). The folded rule must
    therefore set ``display:none`` on ``.tool-marker-details.folded`` itself,
    and must NOT be the old body-only form.
    """
    css = _read_style_css()
    # The whole wrapper is collapsed in the folded state.
    assert re.search(
        r"\.tool-marker-details\.folded\s*\{\s*display:\s*none;?\s*\}",
        css,
    ), (
        ".tool-marker-details.folded must set display:none on the whole wrapper "
        "to remove the empty flex row below a collapsed chip"
    )
    # The old body-only collapse form must be gone (it left the wrapper's
    # flex-basis row + margin claiming empty space).
    assert ".tool-marker-details.folded .tool-marker-details-body" not in css, (
        "the folded state must not hide only the inner body — collapse the "
        "whole .tool-marker-details wrapper instead"
    )
    # The expanded-state base rule keeps its flex-basis / margin-top.
    base = _extract_rule_body(css, ".tool-marker-details")
    assert "flex-basis: 100%" in base and "margin-top: 4px" in base, (
        "the expanded .tool-marker-details rule must keep flex-basis:100% + "
        "margin-top:4px so the detail body wraps onto its own row when expanded"
    )


# ---------------------------------------------------------------------------
# 4. Static guardrail: step prompt templates carry the marker pair
# ---------------------------------------------------------------------------


def test_every_step_prompt_template_carries_marker_pair():
    """Every step-prompt template assembled by the engine must inject the
    ``TEMPLATE_PREFIX_END`` / ``USER_CONTENT_BEGIN`` marker pair, in that
    order, so the running-flow console can split the user message into a
    collapsed system-prompt chip + an expanded user bubble (the Role-Based
    Message Collapse spec requirement).

    The deeper per-prompt position assertions (opener before, anchor after)
    live in ``tests/engine/test_prompt_markers.py``; this test is a higher-
    level health check that no step prompt module silently drops the pair.
    """
    from se3.engine.prompt_markers import TEMPLATE_PREFIX_END, USER_CONTENT_BEGIN
    from se3.engine.steps.analyze import ANALYZE_PROMPT
    from se3.engine.steps.discovery import (
        CONTINUE_DISCOVERY_PROMPT,
        INITIAL_DISCOVERY_PROMPT,
    )
    from se3.engine.steps.implement import (
        FIX_PROMPT,
        IMPLEMENT_GROUP_PROMPT,
        IMPLEMENT_PROMPT,
    )
    from se3.engine.steps.plan import PLAN_PROMPT_HEADER
    from se3.engine.steps.plan_tasks import PLAN_TASKS_PROMPT
    from se3.engine.steps.self_check import SELF_CHECK_PROMPT
    from se3.engine.steps.summarize import SUMMARIZE_PROMPT
    from se3.engine.steps.update_spec import UPDATE_SPEC_PROMPT
    from se3.engine.steps.verify_spec import VERIFY_PROMPT
    from se3.engine.steps.version_analyze import VERSION_ANALYZE_PROMPT

    all_prompts = {
        "IMPLEMENT_PROMPT": IMPLEMENT_PROMPT,
        "IMPLEMENT_GROUP_PROMPT": IMPLEMENT_GROUP_PROMPT,
        "FIX_PROMPT": FIX_PROMPT,
        "ANALYZE_PROMPT": ANALYZE_PROMPT,
        "INITIAL_DISCOVERY_PROMPT": INITIAL_DISCOVERY_PROMPT,
        "CONTINUE_DISCOVERY_PROMPT": CONTINUE_DISCOVERY_PROMPT,
        "PLAN_PROMPT_HEADER": PLAN_PROMPT_HEADER,
        "PLAN_TASKS_PROMPT": PLAN_TASKS_PROMPT,
        "VERIFY_PROMPT": VERIFY_PROMPT,
        "SELF_CHECK_PROMPT": SELF_CHECK_PROMPT,
        "SUMMARIZE_PROMPT": SUMMARIZE_PROMPT,
        "UPDATE_SPEC_PROMPT": UPDATE_SPEC_PROMPT,
        "VERSION_ANALYZE_PROMPT": VERSION_ANALYZE_PROMPT,
    }
    for name, prompt in all_prompts.items():
        assert TEMPLATE_PREFIX_END in prompt, (
            f"{name} is missing TEMPLATE_PREFIX_END marker"
        )
        assert USER_CONTENT_BEGIN in prompt, (
            f"{name} is missing USER_CONTENT_BEGIN marker"
        )
        assert prompt.index(TEMPLATE_PREFIX_END) < prompt.index(
            USER_CONTENT_BEGIN
        ), (
            f"{name}: TEMPLATE_PREFIX_END must precede USER_CONTENT_BEGIN"
        )


# ---------------------------------------------------------------------------
# 5. running-flow-console spec passes structural validation
# ---------------------------------------------------------------------------


def test_running_flow_console_spec_passes_structural_validation():
    """The three new / tightened Requirements (Conversation Strict
    Chronological Order, neutral wording in Unified Intervention Items /
    Docked Persistent Reply Box, Long-Content Wrapping) must keep the spec
    file structurally valid against the spec-format v1 contract.
    """
    from se3.engine.spec_validator import validate_spec_structure

    spec_path = REPO_ROOT / "se3" / "specs" / "running-flow-console" / "spec.md"
    assert spec_path.is_file(), f"missing {spec_path}"
    content = spec_path.read_text(encoding="utf-8")
    result = validate_spec_structure(content, "running-flow-console")
    assert result.passed, "running-flow-console spec failed validation: " + (
        "; ".join(result.errors)
    )
    # Sanity: the three new Requirements are actually present.
    for required_heading in [
        "### Requirement: Conversation Strict Chronological Order",
        "### Requirement: Long-Content Wrapping",
    ]:
        assert required_heading in content, (
            f"running-flow-console spec is missing heading: {required_heading!r}"
        )
