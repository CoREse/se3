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
    """The shared ``makeRawToggle`` result must never be appended at the row
    level via a bare ``rawToggle`` variable.

    For the USER side the raw toggle is appended into an expand area — the
    ``makeUserPromptToggle`` "展开全部" body, or a collapsed chip's expand detail.
    For the ASSISTANT side it is the single fold built below the rendered result
    by ``makeAssistantRawToggle``. The historical bare row-level form
    (``row.appendChild(rawToggle)``) — which leaked the user/assistant raw toggle
    onto the default row — must be gone.

    NOTE: the unified "every conversation message can view raw" principle DOES
    add a guarded row-level ``makeAssistantRawToggle`` for the non-collapsible
    `other` role (see ``test_non_collapsible_path_appends_view_raw_for_other_role``);
    that is a deliberate always-present affordance for `other`, distinct from the
    old bare-variable leak this guard forbids.
    """
    src = _read_app_js()
    assert "row.appendChild(rawToggle)" not in src, (
        "查看原始 must not be appended at the row level via a bare rawToggle "
        "variable — nest it inside the 展开全部 expand area / chip detail (user), "
        "build it below the result via makeAssistantRawToggle (assistant), or "
        "append it guarded by role !== \"assistant\" (other)"
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
# 2c. Static guardrail: unified "every conversation message can view raw"
# ---------------------------------------------------------------------------
#
# The bugfix establishes one principle: all four conversation roles
# (user / assistant / system / other) ALWAYS expose 查看原始, via an
# always-non-null constructor, while the non-conversation synthetic UI
# (group_status markers) stays affordance-free and ``makeRawToggle`` keeps its
# null contract (it is simply no longer called by the chip branch).


def test_assistant_no_result_branch_appends_view_raw():
    """The assistant no-result / unstructurable inline branch MUST append the
    always-present single 查看原始 entry (``makeAssistantRawToggle``) right after
    the inline thinking process (``renderAssistantProcessInline``).

    The inline thinking stays fully shown; the raw toggle is folded by default
    below it. This is the unified principle's coverage of the assistant·inline
    branch that previously had no raw affordance at all.
    """
    body = _extract_js_function_body(_read_app_js(), "renderAssistantBubble")
    inline_idx = body.find("renderAssistantProcessInline(content, norm)")
    assert inline_idx != -1, (
        "the no-result branch must render the inline thinking process"
    )
    tail = body[inline_idx:]
    assert "makeAssistantRawToggle(content, norm)" in tail, (
        "the no-result inline branch must append makeAssistantRawToggle below "
        "the inline thinking process"
    )


def test_chip_branch_dispatches_raw_toggle_by_role():
    """The collapsed-chip branch of ``renderConversationRecord`` MUST dispatch by
    role to an always-non-null raw toggle — ``makeUserRawToggle`` for user
    (envelope fallback, 延续 3870fd8e) and ``makeAssistantRawToggle`` for system
    (content fallback) — instead of the old nullable ``makeRawToggle`` +
    ``if (rawToggle)`` guard, so a system / user chip ALWAYS exposes 查看原始.
    """
    body = _extract_js_function_body(_read_app_js(), "renderConversationRecord")
    assert "makeUserRawToggle(norm)" in body, (
        "chip branch must call makeUserRawToggle for the user role"
    )
    assert "makeAssistantRawToggle(content, norm)" in body, (
        "chip branch must call makeAssistantRawToggle for the system role"
    )
    # The old nullable form must be gone from this function.
    assert "makeRawToggle(norm)" not in body, (
        "chip branch must no longer use the nullable makeRawToggle"
    )
    assert "if (rawToggle)" not in body, (
        "chip branch must no longer guard the toggle append on a nullable result"
    )


def test_non_collapsible_path_appends_view_raw_for_other_role():
    """The non-collapsible (assistant / other) row path of
    ``renderConversationRecord`` MUST append ``makeAssistantRawToggle`` for the
    ``other`` role and for an EMPTY-content assistant turn (which never goes
    through ``renderAssistantBubble``'s in-bubble fold), guarded by
    ``role !== "assistant" || !content`` so an assistant turn WITH content keeps
    its single in-bubble fold and never gets a duplicate row-level toggle.
    """
    body = _extract_js_function_body(_read_app_js(), "renderConversationRecord")
    assert 'role !== "assistant" || !content' in body, (
        "the non-collapsible path must guard the row-level append with "
        'role !== "assistant" || !content so the other role AND an empty-content '
        "assistant turn both get the toggle, without duplicating it on the "
        "assistant-with-content path"
    )
    guard_idx = body.index('role !== "assistant" || !content')
    tail = body[guard_idx:]
    assert "makeAssistantRawToggle(content, norm)" in tail, (
        "the guarded row path must append makeAssistantRawToggle"
    )


def test_make_raw_toggle_retains_return_null_for_non_conversation_paths():
    """``makeRawToggle`` MUST keep its ``return null`` contract even though the
    chip branch no longer calls it — the null behavior is preserved unchanged for
    any non-conversation path that may still rely on it."""
    body = _extract_js_function_body(_read_app_js(), "makeRawToggle")
    assert "return null" in body, (
        "makeRawToggle must keep its 'no raw payload → null' contract"
    )


def test_group_status_record_stays_affordance_free():
    """The non-conversation synthetic UI must stay affordance-free:
    ``renderGroupStatusRecord`` MUST NOT build any raw toggle (no
    ``makeRawToggle`` / ``makeAssistantRawToggle`` / ``makeUserRawToggle`` and no
    ``raw-toggle`` class) — it is a lightweight status marker, not a chat turn.
    """
    body = _extract_js_function_body(_read_app_js(), "renderGroupStatusRecord")
    for forbidden in (
        "makeRawToggle",
        "makeAssistantRawToggle",
        "makeUserRawToggle",
        "raw-toggle",
    ):
        assert forbidden not in body, (
            f"renderGroupStatusRecord must carry NO {forbidden!r} affordance — "
            "it is a non-conversation status marker"
        )


def test_step_event_record_stays_affordance_free():
    """The step_completed / step_failed report-card path
    (``renderStepEventRecord``) is the other non-conversation synthetic UI and
    MUST keep no conversation 查看原始 affordance after the unified change: it
    MUST NOT build any view-raw toggle (no ``makeRawToggle`` /
    ``makeAssistantRawToggle`` / ``makeUserRawToggle``, no ``raw-toggle`` class,
    no 查看原始 button text). Its own raw event chip uses the ``raw-json`` source
    view, which is a different affordance and intentionally left in place.
    """
    body = _extract_js_function_body(_read_app_js(), "renderStepEventRecord")
    for forbidden in (
        "makeRawToggle",
        "makeAssistantRawToggle",
        "makeUserRawToggle",
        "raw-toggle",
        "查看原始",
    ):
        assert forbidden not in body, (
            f"renderStepEventRecord must carry NO {forbidden!r} conversation "
            "view-raw affordance — it is a non-conversation report-card surface"
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


def test_step_report_list_items_wrap_long_text():
    """The ``.step-report__list li`` rule must wrap long text (e.g. a long
    file path in ``Tests Added``) so it never overflows the report card
    boundary.

    Concretely we require a per-character break rule
    (``overflow-wrap: anywhere`` or ``word-break: break-word``) and forbid
    ``overflow-x: auto``.  We do NOT require ``white-space: pre-wrap``
    (unlike the code-block selectors) because list items are normal flow
    text, not ``<pre>`` blocks.

    Regression guard for: long paths in ``tests_added`` overflowing the
    ``#flow-view`` container horizontally.
    """
    body = _extract_rule_body(_read_style_css(), ".step-report__list li")
    assert (
        "overflow-wrap: anywhere" in body
        or "word-break: break-word" in body
    ), (
        ".step-report__list li must declare a per-character break rule "
        "(overflow-wrap: anywhere / word-break: break-word) so long paths "
        "wrap inside the report card"
    )
    assert "overflow-x: auto" not in body, (
        ".step-report__list li must NOT use 'overflow-x: auto'; long text "
        "should wrap rather than open an inner horizontal scrollbar"
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


# ---------------------------------------------------------------------------
# 6. Issues view structural guardrails (G7)
# ---------------------------------------------------------------------------


def _read_index_html() -> str:
    assert (STATIC_DIR / "index.html").is_file(), "missing index.html"
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _read_style_css() -> str:
    assert (STATIC_DIR / "style.css").is_file(), "missing style.css"
    return (STATIC_DIR / "style.css").read_text(encoding="utf-8")


def test_issues_view_html_structure():
    """The issues view must have the expected structural elements."""
    html = _read_index_html()
    # Top-level issues view overlay
    assert 'id="issues-view"' in html, "missing #issues-view"
    # Navigation entry
    assert 'id="issues-btn"' in html, "missing #issues-btn"
    # List and detail panes
    assert 'id="issues-list"' in html, "missing #issues-list"
    assert 'id="issues-detail"' in html, "missing #issues-detail"
    # Filter controls
    assert 'id="issues-show-closed"' in html, "missing #issues-show-closed"
    assert 'id="issues-source-filter"' in html, "missing #issues-source-filter"
    assert 'id="issues-type-filter"' in html, "missing #issues-type-filter"
    # Create button
    assert 'id="issues-create-btn"' in html, "missing #issues-create-btn"
    # Issue modal (create/edit)
    assert 'id="issue-modal"' in html, "missing #issue-modal"
    assert 'id="issue-form"' in html, "missing #issue-form"
    assert 'id="issue-description"' in html, "missing #issue-description"
    # Action modal (close/reopen)
    assert 'id="issue-action-modal"' in html, "missing #issue-action-modal"


def test_issues_view_css_exists():
    """The issues view CSS classes must be defined."""
    css = _read_style_css()
    for cls in [
        ".issues-view",
        ".issues-head",
        ".issues-body",
        ".issues-list-pane",
        ".issues-detail-pane",
        ".issues-list",
        ".issues-toolbar",
        ".issue-item",
        ".issue-detail-header",
        ".issue-detail-desc",
        ".badge-open",
        ".badge-in-progress",
        ".badge-resolved",
        ".badge-closed",
    ]:
        assert cls in css, f"missing CSS class {cls}"


def test_issues_view_mobile_responsive_css():
    """The issues view must have mobile-portrait overflow containment rules."""
    css = _read_style_css()
    # The mobile breakpoint must scope issues-view rules under #issues-view
    assert "#issues-view .issues-body" in css, (
        "missing mobile #issues-view .issues-body max-width rule"
    )
    assert "#issues-view .issues-list-pane" in css, (
        "missing mobile #issues-view .issues-list-pane containment"
    )


def test_issues_panel_state_helper_exists_in_js():
    """The issuesPanelState pure helper must be defined in app.js."""
    src = _read_app_js()
    assert "function issuesPanelState(" in src, (
        "issuesPanelState function not found in app.js"
    )


def test_issue_display_title_helper_exists_in_js():
    """The issueDisplayTitle pure helper must be defined in app.js."""
    src = _read_app_js()
    assert "function issueDisplayTitle(" in src, (
        "issueDisplayTitle function not found in app.js"
    )
    assert "function filterIssues(" in src, (
        "filterIssues function not found in app.js"
    )

# Resume-flow frontend guardrails
# ---------------------------------------------------------------------------


def test_resume_button_css_exists():
    """The ``.btn-resume`` class must be defined in the stylesheet so the
    Resume button renders with the correct inline style."""
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".btn-resume" in css, "missing .btn-resume CSS class"
    # The button must be visually distinct (uppercase label, accent colour).
    assert "text-transform: uppercase" in css


def test_app_js_defines_is_flow_resumable():
    """The pure ``isFlowResumable`` helper must exist and be exported."""
    js = APP_JS.read_text(encoding="utf-8")
    assert "function isFlowResumable(" in js, "missing isFlowResumable function"
    assert "isFlowResumable," in js, "isFlowResumable not exported"


def test_app_js_defines_resume_flow():
    """The ``resumeFlow`` async function must exist and call the resume API."""
    js = APP_JS.read_text(encoding="utf-8")
    assert "async function resumeFlow(" in js, "missing resumeFlow function"
    assert "/resume" in js, "resume endpoint not referenced"


def test_app_js_defines_make_resume_button():
    """The ``makeResumeButton`` factory must exist and create a btn-resume."""
    js = APP_JS.read_text(encoding="utf-8")
    assert "function makeResumeButton(" in js, "missing makeResumeButton function"
    assert 'btn-resume' in js, "btn-resume class not used"


def test_resume_button_appears_in_flow_card():
    """``renderFlowCard`` must call ``makeResumeButton`` to inject the button."""
    js = APP_JS.read_text(encoding="utf-8")
    # renderFlowCard should reference makeResumeButton
    card_section = js[js.index("function renderFlowCard("):]
    card_section = card_section[:card_section.index("\nfunction ")]
    assert "makeResumeButton" in card_section, (
        "renderFlowCard does not call makeResumeButton"
    )


def test_resume_button_appears_in_sidebar():
    """``renderFlowSidebar`` must call ``makeResumeButton`` to add a Resume
    action in the flow detail sidebar."""
    js = APP_JS.read_text(encoding="utf-8")
    sidebar_section = js[js.index("function renderFlowSidebar("):]
    sidebar_section = sidebar_section[:sidebar_section.index("\n// ---")]
    assert "makeResumeButton" in sidebar_section, (
        "renderFlowSidebar does not call makeResumeButton"
    )


def test_resume_button_appears_in_history_list():
    """``renderHistoryList`` must call ``makeResumeButton`` for each session
    card so failed/paused sessions offer a Resume action."""
    js = APP_JS.read_text(encoding="utf-8")
    list_section = js[js.index("function renderHistoryList("):]
    list_section = list_section[:list_section.index("\nfunction historyTitle")]
    assert "makeResumeButton" in list_section, (
        "renderHistoryList does not call makeResumeButton"
    )


# ---------------------------------------------------------------------------
# 4. Static guardrail: start-flow-from-issue launch entry (G4)
# ---------------------------------------------------------------------------


def test_issue_launch_button_in_list_and_detail():
    """``renderIssuesList`` and ``renderIssueDetail`` must both call
    ``makeIssueLaunchButton`` so an issue can be launched from either surface."""
    js = APP_JS.read_text(encoding="utf-8")
    list_section = _extract_js_function_body(js, "renderIssuesList")
    detail_section = _extract_js_function_body(js, "renderIssueDetail")
    assert "makeIssueLaunchButton" in list_section, (
        "renderIssuesList does not call makeIssueLaunchButton"
    )
    assert "makeIssueLaunchButton" in detail_section, (
        "renderIssueDetail does not call makeIssueLaunchButton"
    )


def test_issue_launch_button_respects_launch_model():
    """``makeIssueLaunchButton`` must gate availability through
    ``issueLaunchModel`` so non-open issues render visible-but-disabled."""
    js = APP_JS.read_text(encoding="utf-8")
    body = _extract_js_function_body(js, "makeIssueLaunchButton")
    assert "issueLaunchModel" in body, (
        "makeIssueLaunchButton does not consult issueLaunchModel"
    )
    # The disabled-but-visible contract: the button is disabled (not removed)
    # when the issue is not launchable.
    assert "disabled" in body


def test_issue_launch_modal_present_in_index_html():
    """The start-flow-from-issue modal (with its discovery checkbox) must exist
    in index.html so the launch interaction has a UI."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="issue-launch-modal"' in html
    assert 'id="issue-launch-discover"' in html
    assert 'id="issue-launch-confirm"' in html


# ---------------------------------------------------------------------------
# 5. Static guardrail: reconnect incremental history re-pull wiring (G4)
# ---------------------------------------------------------------------------
#
# The behavioural assertions live in the Node suite (loadFlowConversation /
# openHistorySession against a DOM stub). These guardrails codify the wiring
# contract directly against the JS bytes so a refactor cannot silently drop the
# incremental path back to the old unconditional full re-pull.


def test_reconnect_passes_incremental_to_both_loaders():
    """The WS ``onopen`` reconnect path must re-pull both the running-flow and
    the history-detail views incrementally — i.e. pass ``{ incremental: true }``
    — rather than triggering an unconditional full reload."""
    src = _read_app_js()
    assert re.search(
        r"loadFlowConversation\(\s*state\.selectedFlowId\s*,\s*\{\s*incremental:\s*true\s*\}\s*\)",
        src,
    ), "ws.onopen must call loadFlowConversation(..., { incremental: true })"
    assert re.search(
        r"openHistorySession\(\s*state\.selectedHistoryId\s*,\s*\{\s*incremental:\s*true\s*\}\s*\)",
        src,
    ), "ws.onopen must call openHistorySession(..., { incremental: true })"


def test_incremental_loaders_guard_container_clear_behind_first_open():
    """On the incremental (reconnect) path the loaders MUST NOT clear the
    container or reset ``__convState`` — those resets belong only to the first
    open. The guard is encoded as ``if (!incremental) { … innerHTML = "" … }``
    in both loaders, and each echoes the held progress token via
    ``historySnapshotUrl`` when refreshing incrementally."""
    src = _read_app_js()
    for fn, progress_state in (
        ("loadFlowConversation", "flowConversationProgress"),
        ("openHistorySession", "historyProgress"),
    ):
        body = _extract_js_function_body(src, fn)
        assert "incremental" in body, f"{fn} must accept an incremental option"
        # The destructive resets are gated behind the first-open branch.
        assert "if (!incremental)" in body, (
            f"{fn} must guard its container reset behind `if (!incremental)`"
        )
        assert 'innerHTML = ""' in body, f"{fn} should still clear on first open"
        # The reconnect path echoes the held progress token to request a delta.
        assert "historySnapshotUrl" in body, (
            f"{fn} must request the delta via historySnapshotUrl on reconnect"
        )
        assert f"state.{progress_state}" in body, (
            f"{fn} must echo its held progress token (state.{progress_state})"
        )
        # The shared merge decision helper drives delta-vs-full rendering.
        assert "mergeHistoryResponse" in body, (
            f"{fn} must fold the response through mergeHistoryResponse"
        )
