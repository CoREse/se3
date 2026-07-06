"""Prompt landing points for `se3 code-index search`.

Existence assertions guarding that every LLM-facing place that introduces the
code-index navigation commands also teaches the `search` subcommand as the
grep replacement — so the agent learns to reach for it instead of a raw
`grep se3/code-index.md` (which cannot show a symbol's owning-file path).
"""

from __future__ import annotations

from pathlib import Path

from se3.engine import context_builder
from se3.engine.steps import analyze, discovery, plan

_RUNTIME_ENV_MD = (
    Path(context_builder.__file__).parent / "runtime_environment.md"
)


def test_runtime_environment_md_documents_search():
    text = _RUNTIME_ENV_MD.read_text(encoding="utf-8")
    assert "se3 code-index search" in text
    # Positioned as a grep replacement with grep-aligned syntax.
    assert "code-index.md" in text
    assert "-F" in text and "-m" in text


def test_code_index_injection_header_documents_search(tmp_path: Path):
    # No built map here -> the "not built" branch, but the header (which carries
    # the usage guidance) is prepended unconditionally.
    injection = context_builder.get_code_index_injection(tmp_path)
    assert "se3 code-index search" in injection
    assert "relpath::local_id" in injection


def test_discovery_prompts_document_search():
    for prompt in (discovery.INITIAL_DISCOVERY_PROMPT, discovery.CONTINUE_DISCOVERY_PROMPT):
        assert "se3 code-index search" in prompt
        assert "grep" in prompt


def test_plan_prompt_documents_search():
    assert "se3 code-index search" in plan.PLAN_PROMPT_HEADER
    assert "grep" in plan.PLAN_PROMPT_HEADER


def test_analyze_prompt_documents_search():
    assert "se3 code-index search" in analyze.ANALYZE_PROMPT
    assert "grep" in analyze.ANALYZE_PROMPT
