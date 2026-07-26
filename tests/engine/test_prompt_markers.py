"""Unit tests for prompt_markers helpers."""

from __future__ import annotations

from tianluo.engine.prompt_markers import (
    TEMPLATE_PREFIX_END,
    USER_CONTENT_BEGIN,
    USER_CONTENT_END,
    _marker_pair_present,
    inject_boundary,
    wrap_user_content,
    wrap_user_section,
)
from tianluo.engine.steps.analyze import ANALYZE_PROMPT
from tianluo.engine.steps.discovery import (
    CONTINUE_DISCOVERY_PROMPT,
    INITIAL_DISCOVERY_PROMPT,
)
from tianluo.engine.steps.implement import (
    FIX_PROMPT,
    IMPLEMENT_GROUP_PROMPT,
    IMPLEMENT_PROMPT,
)
from tianluo.engine.steps.plan import PLAN_PROMPT_HEADER
from tianluo.engine.steps.plan_tasks import PLAN_TASKS_PROMPT
from tianluo.engine.steps.self_check import SELF_CHECK_PROMPT
from tianluo.engine.steps.summarize import SUMMARIZE_PROMPT
from tianluo.engine.steps.version_analyze import VERSION_ANALYZE_PROMPT


def test_wrap_user_content_injects_marker_pair():
    out = wrap_user_content("system header\n", "actual task\n")
    assert TEMPLATE_PREFIX_END in out
    assert USER_CONTENT_BEGIN in out
    assert out.index(TEMPLATE_PREFIX_END) < out.index(USER_CONTENT_BEGIN)
    assert "system header" in out and "actual task" in out


def test_wrap_user_content_empty_user_passthrough():
    assert wrap_user_content("sys", "") == "sys"


def test_wrap_user_content_empty_prefix_passthrough():
    assert wrap_user_content("", "user") == "user"


def test_wrap_user_content_idempotent_on_marker_in_prefix():
    once = wrap_user_content("sys", "user")
    twice = wrap_user_content(once, "more")
    assert twice == once + "more"


def test_wrap_user_section_three_segment_order():
    out = wrap_user_section("PREFIX", "USER", "SUFFIX")
    # All three markers present.
    assert TEMPLATE_PREFIX_END in out
    assert USER_CONTENT_BEGIN in out
    assert USER_CONTENT_END in out
    # Strict ordering: prefix < TEMPLATE_END < USER_BEGIN < user_content < USER_END < suffix.
    pos_prefix = out.index("PREFIX")
    pos_template_end = out.index(TEMPLATE_PREFIX_END)
    pos_user_begin = out.index(USER_CONTENT_BEGIN)
    pos_user = out.index("USER")
    pos_user_end = out.index(USER_CONTENT_END)
    pos_suffix = out.index("SUFFIX")
    assert pos_prefix < pos_template_end < pos_user_begin < pos_user < pos_user_end < pos_suffix
    # Exactly one of each marker.
    assert out.count(TEMPLATE_PREFIX_END) == 1
    assert out.count(USER_CONTENT_BEGIN) == 1
    assert out.count(USER_CONTENT_END) == 1


def test_wrap_user_section_idempotent_on_already_wrapped():
    once = wrap_user_section("PREFIX", "USER", "SUFFIX")
    # Re-wrapping by passing the already-wrapped string as prefix MUST NOT
    # inject a second marker triple; the inputs are concatenated as-is.
    twice = wrap_user_section(once, "MORE_USER", "MORE_SUFFIX")
    assert twice == once + "MORE_USER" + "MORE_SUFFIX"
    assert twice.count(TEMPLATE_PREFIX_END) == 1
    assert twice.count(USER_CONTENT_BEGIN) == 1
    assert twice.count(USER_CONTENT_END) == 1


def test_wrap_user_section_idempotent_when_markers_in_user_content():
    inner = wrap_user_section("p", "u", "s")
    # The middle position carries the marker triple; passing it as
    # user_content must still not double-wrap.
    out = wrap_user_section("OUTER_PREFIX", inner, "OUTER_SUFFIX")
    assert out == "OUTER_PREFIX" + inner + "OUTER_SUFFIX"
    assert out.count(TEMPLATE_PREFIX_END) == 1
    assert out.count(USER_CONTENT_END) == 1


def test_wrap_user_section_empty_prefix_and_suffix():
    out = wrap_user_section("", "USER", "")
    # Still emits the three-segment markers so the frontend sees a complete record.
    assert TEMPLATE_PREFIX_END in out
    assert USER_CONTENT_BEGIN in out
    assert USER_CONTENT_END in out
    assert "USER" in out
    # No stray prefix / suffix content.
    assert out.startswith(TEMPLATE_PREFIX_END)
    assert out.endswith(f"{USER_CONTENT_END}\n")


def test_wrap_user_section_empty_user_content_still_wraps():
    out = wrap_user_section("PRE", "", "SUF")
    # Empty middle is still wrapped — the frontend distinguishes "marker present but
    # bubble empty" from "no marker at all" (legacy whole-chip fallback).
    assert TEMPLATE_PREFIX_END in out
    assert USER_CONTENT_BEGIN in out
    assert USER_CONTENT_END in out
    assert out.index(USER_CONTENT_BEGIN) < out.index(USER_CONTENT_END)
    assert "PRE" in out and "SUF" in out


def test_marker_pair_present_helper():
    assert not _marker_pair_present("")
    assert not _marker_pair_present("no markers at all")
    # Two-segment legacy text is NOT considered a full triple.
    legacy = wrap_user_content("sys", "user")
    assert not _marker_pair_present(legacy)
    # Three-segment text is.
    assert _marker_pair_present(wrap_user_section("p", "u", "s"))


def test_wrap_user_section_does_not_affect_legacy_helpers():
    """Old two-segment helpers must still produce their two-marker output
    unchanged after the new three-segment helper was introduced.
    """
    out_wrap = wrap_user_content("sys", "user")
    assert TEMPLATE_PREFIX_END in out_wrap
    assert USER_CONTENT_BEGIN in out_wrap
    # Crucially: the legacy helper MUST NOT emit USER_CONTENT_END.
    assert USER_CONTENT_END not in out_wrap

    out_inject = inject_boundary("sys\n## Anchor\nbody", "## Anchor\n")
    assert TEMPLATE_PREFIX_END in out_inject
    assert USER_CONTENT_BEGIN in out_inject
    assert USER_CONTENT_END not in out_inject


def test_inject_boundary_inserts_before_anchor():
    template = "sys\n## Anchor\nbody"
    out = inject_boundary(template, "## Anchor\n")
    assert TEMPLATE_PREFIX_END in out
    assert USER_CONTENT_BEGIN in out
    # Markers should sit before the anchor text in the result
    assert out.index(TEMPLATE_PREFIX_END) < out.index("## Anchor")


def test_inject_boundary_missing_anchor_is_noop():
    template = "no anchor here"
    assert inject_boundary(template, "## Missing\n") == template


def test_inject_boundary_idempotent():
    template = "sys\n## Anchor\nbody"
    once = inject_boundary(template, "## Anchor\n")
    twice = inject_boundary(once, "## Anchor\n")
    assert twice == once


_ALL_STEP_PROMPTS = {
    "IMPLEMENT_PROMPT": IMPLEMENT_PROMPT,
    "IMPLEMENT_GROUP_PROMPT": IMPLEMENT_GROUP_PROMPT,
    "FIX_PROMPT": FIX_PROMPT,
    "ANALYZE_PROMPT": ANALYZE_PROMPT,
    "INITIAL_DISCOVERY_PROMPT": INITIAL_DISCOVERY_PROMPT,
    "CONTINUE_DISCOVERY_PROMPT": CONTINUE_DISCOVERY_PROMPT,
    "PLAN_PROMPT_HEADER": PLAN_PROMPT_HEADER,
    "PLAN_TASKS_PROMPT": PLAN_TASKS_PROMPT,
    "SELF_CHECK_PROMPT": SELF_CHECK_PROMPT,
    "SUMMARIZE_PROMPT": SUMMARIZE_PROMPT,
    "VERSION_ANALYZE_PROMPT": VERSION_ANALYZE_PROMPT,
}


def test_all_step_prompts_have_exactly_one_marker_pair():
    for name, prompt in _ALL_STEP_PROMPTS.items():
        assert prompt.count(TEMPLATE_PREFIX_END) == 1, (
            f"{name} should contain exactly one TEMPLATE_PREFIX_END marker"
        )
        assert prompt.count(USER_CONTENT_BEGIN) == 1, (
            f"{name} should contain exactly one USER_CONTENT_BEGIN marker"
        )
        # Boundary order: TEMPLATE_PREFIX_END must precede USER_CONTENT_BEGIN
        assert prompt.index(TEMPLATE_PREFIX_END) < prompt.index(USER_CONTENT_BEGIN), (
            f"{name}: TEMPLATE_PREFIX_END must precede USER_CONTENT_BEGIN"
        )


def test_role_opener_is_in_prefix_segment():
    """For every step prompt, the boilerplate role opener (e.g. "You are…",
    "Continue the discovery…") must sit BEFORE TEMPLATE_PREFIX_END so the
    web console can collapse it into the system-prompt chip.
    """
    # Each prompt's recognizable opening boilerplate substring.
    openers = {
        "IMPLEMENT_PROMPT": "You are an expert software engineer.",
        "IMPLEMENT_GROUP_PROMPT": "You are an expert software engineer.",
        "FIX_PROMPT": "You are an expert software engineer.",
        "ANALYZE_PROMPT": "You are an expert software engineering assistant.",
        "INITIAL_DISCOVERY_PROMPT": (
            "You are an expert software engineering assistant in DISCOVERY mode."
        ),
        "CONTINUE_DISCOVERY_PROMPT": (
            "You are an expert software engineering assistant in DISCOVERY mode."
        ),
        "PLAN_PROMPT_HEADER": "You are an expert software engineering assistant.",
        "PLAN_TASKS_PROMPT": "You are an expert software engineering assistant.",
        "SELF_CHECK_PROMPT": "You are an expert code reviewer.",
        "SUMMARIZE_PROMPT": "You are an expert software engineering assistant.",
        "VERSION_ANALYZE_PROMPT": "You are an expert in Semantic Versioning 2.0.0.",
    }
    for name, prompt in _ALL_STEP_PROMPTS.items():
        opener = openers[name]
        assert opener in prompt, f"{name}: expected opener {opener!r} missing"
        assert prompt.index(opener) < prompt.index(TEMPLATE_PREFIX_END), (
            f"{name}: role opener must precede TEMPLATE_PREFIX_END "
            "so it lives in the system-prompt chip portion"
        )


def test_user_content_anchor_is_in_suffix_segment():
    """For each step prompt, the user/task content anchor must sit AFTER
    USER_CONTENT_BEGIN so the web console renders it as an expanded user
    bubble rather than collapsing it into the system-prompt chip.
    """
    # Anchor = the heading or label that introduces the user/task content.
    anchors = {
        "IMPLEMENT_PROMPT": "## Task Description\n",
        "IMPLEMENT_GROUP_PROMPT": "## Task Description\n",
        "FIX_PROMPT": "## Task Description\n",
        "ANALYZE_PROMPT": "Task description:\n",
        # Discovery prompts now use the three-segment marker protocol:
        # the USER_CONTENT region wraps only the user's literal field
        # ({initial_description} for the initial prompt, {user_response}
        # for the continue prompt). Project Context / Available Specs /
        # JSON schema / Guidelines all live in PREFIX or SUFFIX.
        "INITIAL_DISCOVERY_PROMPT": "{initial_description}",
        "CONTINUE_DISCOVERY_PROMPT": "{user_response}",
        "PLAN_PROMPT_HEADER": "## Project Context\n",
        "PLAN_TASKS_PROMPT": "## Task Description\n",
        "SELF_CHECK_PROMPT": "## Task Description\n",
        "SUMMARIZE_PROMPT": "## Task Description\n",
        "VERSION_ANALYZE_PROMPT": "## Task Information\n",
    }
    for name, prompt in _ALL_STEP_PROMPTS.items():
        anchor = anchors[name]
        assert anchor in prompt, f"{name}: expected anchor {anchor!r} missing"
        assert prompt.index(USER_CONTENT_BEGIN) < prompt.index(anchor), (
            f"{name}: user-content anchor must follow USER_CONTENT_BEGIN "
            "so it lives in the user-bubble portion"
        )
