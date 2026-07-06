"""Existence tests for the `se3 code-index search` prompt-guidance rollout.

Issue #262 Part 2: every LLM-facing place that teaches the code-index commands
must also advertise `se3 code-index search <pattern>` as the grep-replacement
for locating code-index items (a raw grep of `se3/code-index.md` loses a
symbol's owning-file path; search prints each hit's full locating path). These
tests pin that guidance to the five known injection points so a future prompt
edit cannot silently drop it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from se3.engine import context_builder
from se3.engine.context_builder import (
    _reset_runtime_environment_cache,
    get_code_index_injection,
)
from se3.engine.steps.analyze import ANALYZE_PROMPT
from se3.engine.steps.discovery import (
    CONTINUE_DISCOVERY_PROMPT,
    INITIAL_DISCOVERY_PROMPT,
)
from se3.engine.steps.plan import PLAN_PROMPT_HEADER

# The command every landing point must name.
SEARCH_CMD = "se3 code-index search"

# The grep-replacement framing (at least one of these grep references must sit
# near the search command so the guidance reads as "use search *instead of*
# grep", not just "here is another command").
GREP_MARKERS = ("grep", "se3/code-index.md")

# The grep-consistent flag subset the guidance must surface.
FLAG_MARKERS = ("-i", "-F", "-m")


def _assert_search_guidance(text: str, *, where: str) -> None:
    """Assert `text` teaches the search command, its grep-replacement role, and
    the grep-consistent flag subset."""
    assert SEARCH_CMD in text, f"{where}: missing `{SEARCH_CMD}` guidance"
    assert any(m in text for m in GREP_MARKERS), (
        f"{where}: search guidance does not frame it as a grep replacement"
    )
    for flag in FLAG_MARKERS:
        assert flag in text, f"{where}: search guidance omits the `{flag}` flag"


def test_runtime_environment_md_advertises_search() -> None:
    """runtime_environment.md 'Locate code via the code-index' section names the
    search command in its command list and its recommended workflow."""
    md_path = Path(context_builder.__file__).parent / "runtime_environment.md"
    text = md_path.read_text(encoding="utf-8")
    _assert_search_guidance(text, where="runtime_environment.md")
    # Recommended-workflow line must reference search too (keyword-search case).
    assert text.count(SEARCH_CMD) >= 2, (
        "runtime_environment.md should mention search in both the command list "
        "and the recommended workflow"
    )


def test_code_index_injection_header_advertises_search(tmp_path: Path) -> None:
    """context_builder.get_code_index_injection header teaches search even when
    the map has not been built (header text is emitted regardless)."""
    # No se3/code-index.md under tmp_path -> the "not built" branch, whose
    # header still carries the standing code-index guidance.
    text = get_code_index_injection(tmp_path)
    _assert_search_guidance(text, where="get_code_index_injection")
    # The header must call out the symbol locating-path payoff explicitly.
    assert "relpath::local_id" in text


def test_discovery_initial_prompt_advertises_search() -> None:
    _assert_search_guidance(INITIAL_DISCOVERY_PROMPT, where="INITIAL_DISCOVERY_PROMPT")


def test_discovery_continue_prompt_advertises_search() -> None:
    _assert_search_guidance(CONTINUE_DISCOVERY_PROMPT, where="CONTINUE_DISCOVERY_PROMPT")


def test_plan_prompt_advertises_search() -> None:
    _assert_search_guidance(PLAN_PROMPT_HEADER, where="PLAN_PROMPT_HEADER")


def test_analyze_prompt_advertises_search() -> None:
    _assert_search_guidance(ANALYZE_PROMPT, where="ANALYZE_PROMPT")


@pytest.fixture(autouse=True)
def _reset_runtime_env_cache():
    """runtime_environment.md is process-cached; reset around each test so a
    stale cache from another module cannot mask a real content change."""
    _reset_runtime_environment_cache()
    yield
    _reset_runtime_environment_cache()
