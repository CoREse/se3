"""Integration tests for the three-segment marker protocol in discovery prompts.

These tests assert the contract that motivated the three-segment marker
extension: after the discovery prompt templates are formatted with concrete
runtime values, the substring strictly bounded by ``USER_CONTENT_BEGIN`` and
``USER_CONTENT_END`` MUST equal the user's literal field
(``initial_description`` for the initial prompt, ``user_response`` for the
continue prompt), with no framework-injected text (Project Context,
Available Specs, JSON schema, Guidelines, …) leaking into the user-content
region.
"""

from __future__ import annotations

from tianluo.engine.chat_history import get_step_history, record_prompt
from tianluo.engine.prompt_markers import (
    TEMPLATE_PREFIX_END,
    USER_CONTENT_BEGIN,
    USER_CONTENT_END,
)
from tianluo.engine.steps.discovery import (
    CONTINUE_DISCOVERY_PROMPT,
    INITIAL_DISCOVERY_PROMPT,
)


# A representative, user-typed initial description that exercises the
# evaluative/inquisitive code path from the discovery spec (Chinese prose
# plus an embedded session reference), so we know unusual characters do not
# break the marker boundary.
_FIXED_INITIAL_DESCRIPTION = (
    "你看一下这个session：tianluo/history/20260520-142159_30166ecb。"
    "youtube下载问题依旧，你是不是没有进行e2e测试？"
)
_FIXED_USER_RESPONSE = (
    "我想要的是把所有下载场景都跑一遍 e2e，而不是改 unit test。"
)


def _extract_user_segment(prompt: str) -> str:
    """Return the substring strictly between USER_CONTENT_BEGIN and USER_CONTENT_END.

    Asserts both markers exist exactly once and are in the canonical order.
    """
    assert prompt.count(TEMPLATE_PREFIX_END) == 1
    assert prompt.count(USER_CONTENT_BEGIN) == 1
    assert prompt.count(USER_CONTENT_END) == 1
    i = prompt.index(TEMPLATE_PREFIX_END)
    j = prompt.index(USER_CONTENT_BEGIN)
    k = prompt.index(USER_CONTENT_END)
    assert i < j < k, (
        "marker order must be TEMPLATE_PREFIX_END < USER_CONTENT_BEGIN < "
        f"USER_CONTENT_END; got positions {i}, {j}, {k}"
    )
    begin = j + len(USER_CONTENT_BEGIN)
    return prompt[begin:k]


def _render_initial(initial_description: str = _FIXED_INITIAL_DESCRIPTION) -> str:
    return INITIAL_DISCOVERY_PROMPT.format(
        initial_description=initial_description,
        round_number=0,
        conversation_history="(No conversation yet)",
        project_context="Project Type: Python (pyproject.toml found)",
        specs_info="Available Specs: base, flow-engine",
        base_spec_content="Base specification content placeholder",
    )


def _render_continue(
    user_response: str = _FIXED_USER_RESPONSE,
    initial_description: str = _FIXED_INITIAL_DESCRIPTION,
) -> str:
    return CONTINUE_DISCOVERY_PROMPT.format(
        initial_description=initial_description,
        round_number=1,
        conversation_history="ASSISTANT: some prior question",
        user_response=user_response,
        project_context="Project Type: Python (pyproject.toml found)",
        specs_info="Available Specs: base, flow-engine",
    )


def test_initial_user_segment_equals_initial_description_stripped():
    prompt = _render_initial()
    user_seg = _extract_user_segment(prompt)
    assert user_seg.strip() == _FIXED_INITIAL_DESCRIPTION.strip()


def test_initial_user_segment_excludes_framework_text():
    prompt = _render_initial()
    user_seg = _extract_user_segment(prompt)
    # Framework boilerplate MUST NOT leak into the user-content region.
    for forbidden in (
        "## Project Context",
        "## Available Specifications",
        "## Base Specification",
        "## Discovery Context",
        "Respond in JSON format",
        "Handling Evaluative/Inquisitive",
        "Guidelines:",
        "You are an expert software engineering assistant",
        "READ-ONLY",
    ):
        assert forbidden not in user_seg, (
            f"forbidden framework substring {forbidden!r} leaked into "
            f"INITIAL user-content region: {user_seg!r}"
        )


def test_continue_user_segment_equals_user_response_stripped():
    prompt = _render_continue()
    user_seg = _extract_user_segment(prompt)
    assert user_seg.strip() == _FIXED_USER_RESPONSE.strip()


def test_continue_user_segment_excludes_framework_text():
    prompt = _render_continue()
    user_seg = _extract_user_segment(prompt)
    for forbidden in (
        "## Project Context",
        "## Available Specifications",
        "## Discovery Context",
        # initial_description echoed back as historical context belongs to
        # PREFIX, NOT to the user-content region.
        _FIXED_INITIAL_DESCRIPTION,
        "Respond in JSON format",
        "Handling Evaluative/Inquisitive",
        "Guidelines:",
        "You are an expert software engineering assistant",
    ):
        assert forbidden not in user_seg, (
            f"forbidden framework substring {forbidden!r} leaked into "
            f"CONTINUE user-content region"
        )


def test_initial_framework_text_lives_in_prefix_or_suffix():
    prompt = _render_initial()
    user_begin = prompt.index(USER_CONTENT_BEGIN)
    user_end = prompt.index(USER_CONTENT_END)
    # Project Context lives in PREFIX.
    assert prompt.index("## Project Context") < user_begin
    # JSON schema / Handling Evaluative / Guidelines live in SUFFIX.
    assert prompt.index("Respond in JSON format") > user_end
    assert prompt.index("Handling Evaluative/Inquisitive") > user_end
    assert prompt.index("Guidelines:") > user_end


def test_continue_framework_text_lives_in_prefix_or_suffix():
    prompt = _render_continue()
    user_begin = prompt.index(USER_CONTENT_BEGIN)
    user_end = prompt.index(USER_CONTENT_END)
    assert prompt.index("## Project Context") < user_begin
    # The initial description echo is part of the prior-context PREFIX.
    assert prompt.index(_FIXED_INITIAL_DESCRIPTION) < user_begin
    assert prompt.index("Respond in JSON format") > user_end
    assert prompt.index("Handling Evaluative/Inquisitive") > user_end
    assert prompt.index("Guidelines:") > user_end


def test_initial_user_segment_with_multiline_description():
    multiline = "line one\n\nline two with `code`\n  indented line"
    prompt = _render_initial(initial_description=multiline)
    user_seg = _extract_user_segment(prompt)
    assert user_seg.strip() == multiline.strip()


def test_continue_user_segment_with_multiline_response():
    multiline = "答复:\n  - 第一点\n  - 第二点"
    prompt = _render_continue(user_response=multiline)
    user_seg = _extract_user_segment(prompt)
    assert user_seg.strip() == multiline.strip()


def test_continue_initial_description_appears_only_in_prefix():
    # When the same string is used for both initial_description (echoed in
    # PREFIX) and user_response (the user-content region), the substring
    # ought to appear in BOTH prefix and content. This test guards the
    # "echo lives in prefix" invariant by checking the first occurrence
    # comes before USER_CONTENT_BEGIN.
    same = "this is repeated text"
    prompt = _render_continue(
        user_response=same, initial_description=same,
    )
    first = prompt.find(same)
    user_begin = prompt.index(USER_CONTENT_BEGIN)
    assert first < user_begin


# ---------------------------------------------------------------------------
# End-to-end: markers survive the record_prompt -> history round-trip
# ---------------------------------------------------------------------------
#
# The template-level tests above prove the prompt strings are *assembled*
# with the three-segment markers. These tests close the loop the running-flow
# console actually depends on: the discovery prompt sent to the LLM is
# persisted verbatim into the per-step jsonl history (role="user"), so that
# the daemon history reader / frontend `splitUserPromptByMarker` has the full
# marker sequence to split on. A regression that strips or rewrites the
# recorded prompt would silently break the web user-content bubble even though
# the in-memory template is still correct — hence a dedicated round-trip test.

_FLOW_ID = "20260521-100145_e2emarkr"
_STEP_ID = "00_discovery_e2e"


def _record_and_read_back(project_root, prompt: str):
    record_prompt(
        project_root=project_root,
        flow_id=_FLOW_ID,
        step_id=_STEP_ID,
        step_type="discovery",
        prompt=prompt,
        attempt=0,
    )
    session = get_step_history(project_root, _FLOW_ID, _STEP_ID)
    assert session is not None, "history session must exist after record_prompt"
    assert len(session.messages) == 1
    return session.messages[0]


def test_initial_discovery_prompt_persists_markers_in_history(tmp_path):
    prompt = _render_initial()
    msg = _record_and_read_back(tmp_path, prompt)

    # The recorded turn is a user prompt — the role the frontend keys its
    # marker-aware rendering off of.
    assert msg.role == "user"
    # All three markers survive the round-trip in canonical order, so the
    # frontend can perform the full three-segment split.
    user_seg = _extract_user_segment(msg.content)
    assert user_seg.strip() == _FIXED_INITIAL_DESCRIPTION.strip()
    # Framework boilerplate stays in prefix/suffix, not in the user region.
    assert "## Project Context" not in user_seg
    assert "Respond in JSON format" not in user_seg


def test_continue_discovery_prompt_persists_markers_in_history(tmp_path):
    prompt = _render_continue()
    msg = _record_and_read_back(tmp_path, prompt)

    assert msg.role == "user"
    user_seg = _extract_user_segment(msg.content)
    assert user_seg.strip() == _FIXED_USER_RESPONSE.strip()
    # The round 0 initial_description is historical context in PREFIX, never
    # in the user-content region.
    assert _FIXED_INITIAL_DESCRIPTION not in user_seg


def test_history_user_content_round_trips_byte_for_byte(tmp_path):
    # The user's literal multiline input must survive the persistence layer
    # exactly (the frontend only trims leading/trailing newline join-glue).
    multiline = "答复:\n  - 第一点\n  - 第二点 with `inline code`"
    prompt = _render_continue(user_response=multiline)
    msg = _record_and_read_back(tmp_path, prompt)
    user_seg = _extract_user_segment(msg.content)
    assert user_seg.strip() == multiline.strip()
