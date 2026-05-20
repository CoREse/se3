"""Unit tests for prompt_markers helpers."""

from __future__ import annotations

from se3.engine.prompt_markers import (
    TEMPLATE_PREFIX_END,
    USER_CONTENT_BEGIN,
    inject_boundary,
    wrap_user_content,
)
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


def test_all_step_prompts_have_exactly_one_marker_pair():
    prompts = {
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
    for name, prompt in prompts.items():
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
