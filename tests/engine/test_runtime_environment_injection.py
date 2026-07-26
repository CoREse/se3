"""Tests for context_builder.get_runtime_environment_injection.

Covers default whitelist behavior, FORBIDDEN precedence, yaml override
narrowing/widening, missing-markdown graceful fallback, and malformed yaml
values.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from tianluo.engine import context_builder
from tianluo.engine.context_builder import (
    RUNTIME_ENV_INJECTION_DEFAULT_STEPS,
    RUNTIME_ENV_INJECTION_FORBIDDEN_STEPS,
    _reset_runtime_environment_cache,
    get_runtime_environment_injection,
)

HEADING = "## tianluo Runtime Environment"

# Whitelist commands the injection must advertise.
WHITELIST_COMMANDS = [
    "luo history list",
    "luo history show",
    "luo history archived",
    "luo issue list",
    "luo issue show",
]

# Blacklist commands the injection must warn about.
BLACKLIST_COMMANDS = [
    "luo history restore",
    "luo issue create",
    "luo issue reset",
    "luo salvage",
    "luo merge",
    "luo sync",
    "luo init",
]

# Free-form file path references.
PATH_REFERENCES = [
    "tianluo/history/<flow_id>",
    "tianluo/issues/",
]

# Workflow recommendation feature phrases.
WORKFLOW_HINTS = [
    "先用 `luo history list` 找到",
    "先用 `luo issue list`",
]


@pytest.fixture(autouse=True)
def _reset_cache():
    """Reset the markdown cache between tests so file-removal tests work."""
    _reset_runtime_environment_cache()
    yield
    _reset_runtime_environment_cache()


def test_default_whitelist_returns_full_content(tmp_path):
    """All whitelisted steps should return non-empty injection containing every
    required string (heading, whitelist commands, blacklist commands, path
    references, both workflow recommendation phrases)."""
    result = get_runtime_environment_injection("plan", tmp_path)
    assert result.startswith("\n\n" + HEADING)
    for cmd in WHITELIST_COMMANDS:
        assert cmd in result, f"missing whitelist command: {cmd}"
    for cmd in BLACKLIST_COMMANDS:
        assert cmd in result, f"missing blacklist command: {cmd}"
    for path in PATH_REFERENCES:
        assert path in result, f"missing path reference: {path}"
    for hint in WORKFLOW_HINTS:
        assert hint in result, f"missing workflow hint: {hint}"
    # Blacklist preamble must be present verbatim.
    assert "以下 luo 命令存在但" in result
    assert "除非用户在当前会话中" in result


def test_forbidden_steps_always_return_empty_even_with_yaml(tmp_path):
    """commit / version_analyze are FORBIDDEN. Even if the user explicitly
    lists them in tianluo.yaml the function must still return ``""``."""
    (tmp_path / "tianluo.yaml").write_text(
        "runtime_environment_injection:\n  steps: [commit, version_analyze, plan]\n",
        encoding="utf-8",
    )
    for step in ("commit", "version_analyze"):
        assert step in RUNTIME_ENV_INJECTION_FORBIDDEN_STEPS
        assert get_runtime_environment_injection(step, tmp_path) == ""
    # plan from the same override should still receive injection.
    assert HEADING in get_runtime_environment_injection("plan", tmp_path)


def test_yaml_override_narrows_and_widens_whitelist(tmp_path):
    """yaml `runtime_environment_injection.steps` replaces the default list."""
    # Narrow to only one step
    (tmp_path / "tianluo.yaml").write_text(
        "runtime_environment_injection:\n  steps: [my_custom_step]\n",
        encoding="utf-8",
    )
    # implement is in default list but not in override -> empty
    assert get_runtime_environment_injection("implement", tmp_path) == ""
    # Custom step gets it
    result = get_runtime_environment_injection("my_custom_step", tmp_path)
    assert HEADING in result

    # Widening: add a step to the existing defaults via override
    (tmp_path / "tianluo.yaml").write_text(
        "runtime_environment_injection:\n"
        "  steps: [analyze, plan, plan_tasks, implement, verify_spec, "
        "update_spec, self_check, discovery, summarize, my_extra_step]\n",
        encoding="utf-8",
    )
    assert HEADING in get_runtime_environment_injection("my_extra_step", tmp_path)
    # FORBIDDEN still wins even after widening attempts
    assert get_runtime_environment_injection("commit", tmp_path) == ""


def test_missing_markdown_returns_empty_and_warns_once(tmp_path, caplog, monkeypatch):
    """When runtime_environment.md cannot be read the function must return
    ``""`` rather than raise, and must emit only one warning even across
    multiple calls."""
    # Point the cache loader at a path that does not exist by monkey-patching
    # the helper to look at a temp dir without the file.
    bogus_dir = tmp_path / "no-markdown-here"
    bogus_dir.mkdir()

    original_loader = context_builder._load_runtime_environment_markdown

    def _broken_path_loader():
        # Reimplement the loader but pointing at a non-existent path so we
        # exercise the warning path without actually deleting the real file.
        global_cache = context_builder._runtime_env_markdown_cache
        if global_cache is not context_builder._RUNTIME_ENV_MARKDOWN_UNSET:
            return global_cache
        md_path = bogus_dir / "runtime_environment.md"
        try:
            content = md_path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError) as e:
            if not context_builder._runtime_env_warning_logged:
                context_builder.logger.warning(
                    "runtime_environment.md missing or unreadable at %s: %s",
                    md_path, e,
                )
                context_builder._runtime_env_warning_logged = True
            content = ""
        context_builder._runtime_env_markdown_cache = content
        return content

    monkeypatch.setattr(
        context_builder, "_load_runtime_environment_markdown", _broken_path_loader
    )

    with caplog.at_level(logging.WARNING, logger="tianluo.engine.context_builder"):
        result1 = get_runtime_environment_injection("plan", tmp_path)
        result2 = get_runtime_environment_injection("implement", tmp_path)
        result3 = get_runtime_environment_injection("self_check", tmp_path)

    assert result1 == ""
    assert result2 == ""
    assert result3 == ""
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "runtime_environment.md" in warnings[0].getMessage()


def test_null_yaml_falls_back_to_defaults(tmp_path):
    """``runtime_environment_injection: null`` (explicit null) must not crash;
    function falls back to default whitelist."""
    (tmp_path / "tianluo.yaml").write_text(
        "runtime_environment_injection: null\n",
        encoding="utf-8",
    )
    assert HEADING in get_runtime_environment_injection("plan", tmp_path)


def test_non_list_yaml_override_ignored(tmp_path):
    """If the user types ``steps: plan`` instead of ``steps: [plan]``, the
    string is ignored and defaults are used (so `'p' in 'plan'` substring
    semantics never leak)."""
    (tmp_path / "tianluo.yaml").write_text(
        "runtime_environment_injection:\n  steps: plan\n",
        encoding="utf-8",
    )
    # Defaults still apply -> plan whitelisted.
    assert HEADING in get_runtime_environment_injection("plan", tmp_path)
    # A non-default step like 'not_a_real_step' must NOT match (would match if
    # the bare-string was treated as a substring whitelist).
    assert get_runtime_environment_injection("not_a_real_step", tmp_path) == ""


def test_defaults_cover_expected_steps():
    """Sanity: the default whitelist covers every LLM-free-decision step."""
    for step in [
        "analyze",
        "plan",
        "plan_tasks",
        "implement",
        "verify_spec",
        "update_spec",
        "self_check",
        "discovery",
        "summarize",
    ]:
        assert step in RUNTIME_ENV_INJECTION_DEFAULT_STEPS
    # Mechanical steps are absent from defaults and forbidden.
    for step in ("commit", "version_analyze"):
        assert step in RUNTIME_ENV_INJECTION_FORBIDDEN_STEPS
        assert step not in RUNTIME_ENV_INJECTION_DEFAULT_STEPS


def test_markdown_is_cached_across_calls(tmp_path, monkeypatch):
    """The markdown should be read exactly once per process: subsequent calls
    must not hit the filesystem again."""
    # Force a fresh load
    _reset_runtime_environment_cache()

    real_read_text = Path.read_text
    call_count = {"n": 0}

    def counting_read_text(self, *args, **kwargs):
        if self.name == "runtime_environment.md":
            call_count["n"] += 1
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    # Three calls — only one read should happen.
    get_runtime_environment_injection("plan", tmp_path)
    get_runtime_environment_injection("implement", tmp_path)
    get_runtime_environment_injection("self_check", tmp_path)

    assert call_count["n"] == 1


def test_runtime_env_appears_in_prompt_end_to_end(tmp_path, monkeypatch):
    """End-to-end: implement_handler must produce a final prompt that contains
    the runtime environment injection (heading + whitelist + blacklist
    representatives). Mirrors the existing implement_handler test pattern from
    tests/test_implement.py: patch LLMCaller and inspect the prompt argument
    passed to ``caller.call(...)``.
    """
    from unittest.mock import MagicMock, patch

    from tianluo.engine.models import FlowInstance, Step, StepType

    single_group = [
        {
            "group_id": "G1",
            "group_order": 1,
            "depends_on": [],
            "tasks": [{"id": 1, "description": "Task 1", "estimated_loc": 50}],
        }
    ]
    step = Step(
        step_type=StepType.IMPLEMENT,
        step_id="test-implement-injection",
        inputs={
            "task_description": "Test runtime env injection",
            "task_type": "feature",
            "task_groups": single_group,
            "design_doc": {},
            "spec_content": {},
        },
    )
    flow = FlowInstance(
        task_description="Test runtime env injection",
        change_path=tmp_path / "tianluo",
    )

    captured_prompts: list[str] = []
    mock_caller = MagicMock()

    def _capture_call(prompt, **kwargs):
        captured_prompts.append(prompt)
        return "{}"

    mock_caller.call.side_effect = _capture_call

    with patch("tianluo.engine.steps.implement.LLMCaller", return_value=mock_caller), \
         patch("tianluo.engine.steps.implement.parse_json_response", return_value={
             "files_changed": ["a.py"],
             "tests_added": [],
             "test_mapping": {},
             "summary": "ok",
             "completion_status": "complete",
             "incomplete_tasks": [],
             "restricted_edits": [],
         }):
        from tianluo.engine.steps.implement import implement_handler
        implement_handler(step, flow)

    assert captured_prompts, "implement_handler should have invoked the LLM at least once"
    prompt = captured_prompts[0]
    # Heading + representative whitelist + representative blacklist commands
    # must all appear in the final prompt sent to the LLM.
    assert HEADING in prompt
    assert "luo history list" in prompt
    assert "luo issue list" in prompt
    assert "luo salvage" in prompt
