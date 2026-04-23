"""Tests for language configuration and language injection."""

from __future__ import annotations

import pytest
from pathlib import Path

from se3.config import LanguageConfig, load_language_config, get_language_instruction
from se3.engine.context_builder import (
    get_step_language_instruction,
    HUMAN_FACING_STEPS,
    SPEC_STEPS,
)


# --- LanguageConfig loading tests ---


class TestLanguageConfigLoad:
    """Tests for LanguageConfig.load() and load_language_config()."""

    def test_defaults_when_no_file(self, tmp_path):
        """Both fields default to None when se3.yaml doesn't exist."""
        config = LanguageConfig.load(tmp_path)
        assert config.language is None
        assert config.spec_language is None

    def test_defaults_when_no_language_section(self, tmp_path):
        """Both fields default to None when language section is missing."""
        (tmp_path / "se3.yaml").write_text("version:\n  enabled: true\n")
        config = LanguageConfig.load(tmp_path)
        assert config.language is None
        assert config.spec_language is None

    def test_defaults_when_null_values(self, tmp_path):
        """Both fields are None when explicitly set to null."""
        (tmp_path / "se3.yaml").write_text(
            "language:\n  language: null\n  spec_language: null\n"
        )
        config = LanguageConfig.load(tmp_path)
        assert config.language is None
        assert config.spec_language is None

    def test_both_set(self, tmp_path):
        """Both fields are loaded when set."""
        (tmp_path / "se3.yaml").write_text(
            "language:\n  language: zh-CN\n  spec_language: en\n"
        )
        config = LanguageConfig.load(tmp_path)
        assert config.language == "zh-CN"
        assert config.spec_language == "en"

    def test_only_language_set(self, tmp_path):
        """Only language is set, spec_language defaults to None."""
        (tmp_path / "se3.yaml").write_text(
            "language:\n  language: zh-CN\n"
        )
        config = LanguageConfig.load(tmp_path)
        assert config.language == "zh-CN"
        assert config.spec_language is None

    def test_only_spec_language_set(self, tmp_path):
        """Only spec_language is set, language defaults to None."""
        (tmp_path / "se3.yaml").write_text(
            "language:\n  spec_language: en\n"
        )
        config = LanguageConfig.load(tmp_path)
        assert config.language is None
        assert config.spec_language == "en"

    def test_load_language_config_convenience(self, tmp_path):
        """load_language_config() convenience function works."""
        (tmp_path / "se3.yaml").write_text(
            "language:\n  language: ja\n  spec_language: ko\n"
        )
        config = load_language_config(tmp_path)
        assert config.language == "ja"
        assert config.spec_language == "ko"

    def test_invalid_yaml(self, tmp_path):
        """Gracefully handles invalid YAML."""
        (tmp_path / "se3.yaml").write_text("{{invalid yaml")
        config = LanguageConfig.load(tmp_path)
        assert config.language is None
        assert config.spec_language is None

    def test_language_section_not_dict(self, tmp_path):
        """Handles case where language section is a scalar instead of dict."""
        (tmp_path / "se3.yaml").write_text("language: zh-CN\n")
        config = LanguageConfig.load(tmp_path)
        assert config.language is None
        assert config.spec_language is None


# --- get_language_instruction tests ---


class TestGetLanguageInstruction:
    """Tests for get_language_instruction()."""

    def test_none_returns_empty(self):
        """Returns empty string when language is None."""
        assert get_language_instruction(None) == ""

    def test_empty_string_returns_empty(self):
        """Returns empty string when language is empty string."""
        assert get_language_instruction("") == ""

    def test_returns_instruction_for_language(self):
        """Returns instruction containing the language code."""
        result = get_language_instruction("zh-CN")
        assert "zh-CN" in result
        assert "MUST" in result

    def test_includes_context(self):
        """Includes context in instruction when provided."""
        result = get_language_instruction("en", "summarize")
        assert "en" in result
        assert "summarize" in result

    def test_no_context(self):
        """Works without context."""
        result = get_language_instruction("ja")
        assert "ja" in result


# --- Step language instruction tests ---


class TestGetStepLanguageInstruction:
    """Tests for get_step_language_instruction() in context_builder."""

    def _write_config(self, tmp_path, language=None, spec_language=None,
                      confirmation_enabled=False, confirmation_steps=None,
                      reviewer="human"):
        """Helper to write se3.yaml using the per-step confirmation schema.

        ``confirmation_enabled`` retains the old keyword for test
        readability, but maps to the new schema by listing the steps in
        ``confirmation.steps`` only when enabled.
        """
        lines = []
        lines.append("language:")
        lines.append(f"  language: {language}" if language else "  language: null")
        lines.append(f"  spec_language: {spec_language}" if spec_language else "  spec_language: null")
        if confirmation_enabled and confirmation_steps:
            lines.append("confirmation:")
            lines.append("  steps:")
            for s in confirmation_steps:
                if reviewer == "human":
                    lines.append(f"    {s}: {{reviewer: human}}")
                else:
                    # Non-human reviewer falls back to llm_caller.defaults
                    # when omitted; explicit None keeps the test focused
                    # on the language-injection behavior.
                    lines.append(f"    {s}: {{}}")
        (tmp_path / "se3.yaml").write_text("\n".join(lines) + "\n")

    def test_summarize_uses_general_language(self, tmp_path):
        """Summarize step uses config.language."""
        self._write_config(tmp_path, language="zh-CN")
        result = get_step_language_instruction("summarize", tmp_path)
        assert "zh-CN" in result

    def test_discovery_uses_general_language(self, tmp_path):
        """Discovery step uses config.language."""
        self._write_config(tmp_path, language="zh-CN")
        result = get_step_language_instruction("discovery", tmp_path)
        assert "zh-CN" in result

    def test_update_spec_uses_spec_language(self, tmp_path):
        """update_spec step uses config.spec_language."""
        self._write_config(tmp_path, spec_language="en")
        result = get_step_language_instruction("update_spec", tmp_path)
        assert "en" in result

    def test_update_spec_ignores_general_language(self, tmp_path):
        """update_spec uses spec_language, not general language."""
        self._write_config(tmp_path, language="zh-CN", spec_language=None)
        result = get_step_language_instruction("update_spec", tmp_path)
        assert result == ""

    def test_implement_no_instruction(self, tmp_path):
        """implement step gets no language instruction."""
        self._write_config(tmp_path, language="zh-CN", spec_language="en")
        result = get_step_language_instruction("implement", tmp_path)
        assert result == ""

    def test_analyze_no_instruction(self, tmp_path):
        """analyze step gets no language instruction."""
        self._write_config(tmp_path, language="zh-CN")
        result = get_step_language_instruction("analyze", tmp_path)
        assert result == ""

    def test_test_no_instruction(self, tmp_path):
        """test step gets no language instruction."""
        self._write_config(tmp_path, language="zh-CN")
        result = get_step_language_instruction("test", tmp_path)
        assert result == ""

    def test_null_config_no_instruction_anywhere(self, tmp_path):
        """No language instructions when both configs are null."""
        self._write_config(tmp_path)
        for step in ["summarize", "discovery", "update_spec", "implement", "analyze"]:
            result = get_step_language_instruction(step, tmp_path)
            assert result == "", f"Expected no instruction for {step}"

    def test_confirmed_step_uses_general_language(self, tmp_path):
        """Steps with human confirmation use general language."""
        self._write_config(
            tmp_path,
            language="zh-CN",
            confirmation_enabled=True,
            confirmation_steps=["plan"],
            reviewer="human",
        )
        result = get_step_language_instruction("plan", tmp_path)
        assert "zh-CN" in result

    def test_confirmed_step_llm_reviewer_uses_language(self, tmp_path):
        """Steps with LLM confirmation also use general language."""
        self._write_config(
            tmp_path,
            language="zh-CN",
            confirmation_enabled=True,
            confirmation_steps=["plan"],
            reviewer="llm",
        )
        result = get_step_language_instruction("plan", tmp_path)
        assert "zh-CN" in result

    def test_confirmed_step_disabled_no_instruction(self, tmp_path):
        """Steps with disabled confirmation don't use general language."""
        self._write_config(
            tmp_path,
            language="zh-CN",
            confirmation_enabled=False,
            confirmation_steps=["plan"],
        )
        result = get_step_language_instruction("plan", tmp_path)
        assert result == ""

    def test_no_yaml_file(self, tmp_path):
        """Works when se3.yaml doesn't exist."""
        result = get_step_language_instruction("summarize", tmp_path)
        assert result == ""


# --- ContextBuilder integration tests ---


class TestContextBuilderLanguageInjection:
    """Tests for language injection via get_step_language_instruction."""

    def test_language_instruction_includes_language(self, tmp_path):
        """get_step_language_instruction returns language for human-facing steps."""
        (tmp_path / "se3.yaml").write_text(
            "language:\n  language: zh-CN\n  spec_language: null\n"
        )
        from se3.engine.context_builder import get_step_language_instruction
        instruction = get_step_language_instruction("summarize", tmp_path)
        assert "zh-CN" in instruction

    def test_no_language_for_implement(self, tmp_path):
        """get_step_language_instruction returns empty for implement step."""
        (tmp_path / "se3.yaml").write_text(
            "language:\n  language: zh-CN\n  spec_language: en\n"
        )
        from se3.engine.context_builder import get_step_language_instruction
        instruction = get_step_language_instruction("implement", tmp_path)
        assert "MUST respond in" not in instruction

    def test_spec_language_for_update_spec(self, tmp_path):
        """get_step_language_instruction uses spec_language for update_spec step."""
        (tmp_path / "se3.yaml").write_text(
            "language:\n  language: zh-CN\n  spec_language: en\n"
        )
        from se3.engine.context_builder import get_step_language_instruction
        instruction = get_step_language_instruction("update_spec", tmp_path)
        assert "en" in instruction
        # Should NOT contain zh-CN (that's for human-facing steps)
        assert "zh-CN" not in instruction


# --- Constants tests ---


class TestConstants:
    """Verify the step category constants are correct."""

    def test_human_facing_steps(self):
        assert "summarize" in HUMAN_FACING_STEPS
        assert "discovery" in HUMAN_FACING_STEPS
        assert "implement" not in HUMAN_FACING_STEPS

    def test_spec_steps(self):
        assert "update_spec" in SPEC_STEPS
        assert "implement" not in SPEC_STEPS
