"""Tests for per-step agent override loading (load_step_agents).

Covers the new schema: ``llm_caller.steps.<step>`` is a list of agent
name references into the top-level ``agents`` registry. Inline dict
entries are deprecated — they warn and are treated as "no override".

- Missing declaration returns None (fall back to default chain).
- Legal declaration (name list) returns normalized+sorted list.
- Project-level declaration fully replaces global declaration.
- Empty list returns None + warning.
- Structurally invalid values return None + warning.
- Inline dict entries warn + are skipped (breaking change).
- Unknown agent names raise ValueError with helpful message.
- A declaration for one step does not affect other (unaffected) steps.
"""

from unittest.mock import patch

import pytest

import se3.config as _cfg
from se3.config import (
    load_self_check_resolution,
    load_step_agents,
    resolve_agents,
)


@pytest.fixture(autouse=True)
def _reset_module_caches():
    """Clear module-level dedup caches before + after each test.

    Without this, the first test to log a warning for a given
    ``(source_label, unknown_keys)`` tuple poisons every subsequent test
    that expects to observe the same warning — producing an
    ordering-dependent false pass.
    """
    _cfg._warned_unknown_step_keys_for.clear()
    _cfg._warned_non_dict_llm_caller_for.clear()
    _cfg._warned_list_agents_for.clear()
    _cfg._warned_claude_commands_ignored_for.clear()
    _cfg._warned_claude_commands_deprecated_for.clear()
    _cfg._warned_agent_priority_deprecated_for.clear()
    yield
    _cfg._warned_unknown_step_keys_for.clear()
    _cfg._warned_non_dict_llm_caller_for.clear()
    _cfg._warned_list_agents_for.clear()
    _cfg._warned_claude_commands_ignored_for.clear()
    _cfg._warned_claude_commands_deprecated_for.clear()
    _cfg._warned_agent_priority_deprecated_for.clear()


_REGISTRY_YAML = """agents:
  primary: {cmd: claude, priority: 10}
  backup: {cmd: claude-dev, priority: 5}
  opus: {cmd: claude-opus, priority: 20}
  small: {cmd: hclaude, priority: 1}
"""


class TestLoadStepAgentsNoConfig:
    def test_returns_none_when_no_config(self, tmp_path):
        with patch("se3.config.Path.home", return_value=tmp_path):
            assert load_step_agents(tmp_path, "implement") is None

    def test_returns_none_when_step_not_declared(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            _REGISTRY_YAML + """llm_caller:
  steps:
    implement: [opus, primary]
"""
        )
        with patch("se3.config.Path.home", return_value=tmp_path):
            # 'plan' is not declared; should return None even though
            # 'implement' is.
            assert load_step_agents(tmp_path, "plan") is None

    def test_returns_none_when_step_type_empty(self, tmp_path):
        with patch("se3.config.Path.home", return_value=tmp_path):
            assert load_step_agents(tmp_path, "") is None
            assert load_step_agents(tmp_path, None) is None


class TestLoadStepAgentsLegalDeclaration:
    def test_name_list_preserves_written_order(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            _REGISTRY_YAML + """llm_caller:
  steps:
    implement: [small, opus, primary]
"""
        )
        with patch("se3.config.Path.home", return_value=tmp_path):
            agents = load_step_agents(tmp_path, "implement")

        assert agents is not None
        # Written order is preserved — priority is ignored for ordering.
        assert [a["name"] for a in agents] == ["small", "opus", "primary"]
        assert all(a["type"] == "claude-code" for a in agents)

    def test_single_name_reference(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            _REGISTRY_YAML + """llm_caller:
  steps:
    summarize: [small]
"""
        )
        with patch("se3.config.Path.home", return_value=tmp_path):
            agents = load_step_agents(tmp_path, "summarize")

        assert agents is not None
        assert len(agents) == 1
        assert agents[0]["name"] == "small"
        assert agents[0]["cmd"] == "hclaude"


class TestProjectOverridesGlobal:
    def test_project_replaces_global_for_same_step(self, tmp_path):
        # Global declares an override for implement using its own agents.
        global_dir = tmp_path / ".se3"
        global_dir.mkdir()
        (global_dir / "config.yaml").write_text(
            """agents:
  global_agent: {cmd: global-claude, priority: 10}
llm_caller:
  steps:
    implement: [global_agent]
"""
        )
        # Project also declares an override for implement using its own agents.
        (tmp_path / "se3.yaml").write_text(
            """agents:
  project_agent: {cmd: project-claude, priority: 5}
llm_caller:
  steps:
    implement: [project_agent]
"""
        )
        with patch("se3.config.Path.home", return_value=tmp_path):
            agents = load_step_agents(tmp_path, "implement")

        assert agents is not None
        assert len(agents) == 1
        assert agents[0]["name"] == "project_agent"

    def test_global_used_when_project_does_not_declare_step(self, tmp_path):
        global_dir = tmp_path / ".se3"
        global_dir.mkdir()
        (global_dir / "config.yaml").write_text(
            """agents:
  global_agent: {cmd: global-claude, priority: 10}
llm_caller:
  steps:
    implement: [global_agent]
"""
        )
        # Project declares nothing under llm_caller.
        (tmp_path / "se3.yaml").write_text(
            """agents:
  foo: {cmd: claude}
"""
        )
        with patch("se3.config.Path.home", return_value=tmp_path):
            agents = load_step_agents(tmp_path, "implement")

        assert agents is not None
        assert agents[0]["name"] == "global_agent"


class TestInvalidDeclarations:
    def test_empty_list_returns_none(self, tmp_path, caplog):
        (tmp_path / "se3.yaml").write_text(
            _REGISTRY_YAML + """llm_caller:
  steps:
    implement: []
"""
        )
        with patch("se3.config.Path.home", return_value=tmp_path):
            import logging
            with caplog.at_level(logging.WARNING, logger="se3.config"):
                agents = load_step_agents(tmp_path, "implement")

        assert agents is None
        assert any("llm_caller.steps.implement" in rec.message
                   for rec in caplog.records)

    def test_non_list_returns_none(self, tmp_path, caplog):
        # A bare string instead of a list.
        (tmp_path / "se3.yaml").write_text(
            _REGISTRY_YAML + """llm_caller:
  steps:
    implement: "claude"
"""
        )
        with patch("se3.config.Path.home", return_value=tmp_path):
            import logging
            with caplog.at_level(logging.WARNING, logger="se3.config"):
                agents = load_step_agents(tmp_path, "implement")

        assert agents is None
        assert any("not a list" in rec.message for rec in caplog.records)

    def test_non_string_entries_filtered(self, tmp_path, caplog):
        # Mixed list: one valid name, one integer (junk).
        (tmp_path / "se3.yaml").write_text(
            _REGISTRY_YAML + """llm_caller:
  steps:
    implement:
      - opus
      - 42
"""
        )
        with patch("se3.config.Path.home", return_value=tmp_path):
            import logging
            with caplog.at_level(logging.WARNING, logger="se3.config"):
                agents = load_step_agents(tmp_path, "implement")

        # Valid name survives; junk entry dropped with warning.
        assert agents is not None
        assert len(agents) == 1
        assert agents[0]["name"] == "opus"
        assert any("non-str entry" in rec.message for rec in caplog.records)


class TestInlineDictEntriesDeprecated:
    """Inline dict form is a breaking change — entries are skipped and
    warned. The whole override becomes 'no override' when no valid name
    entries remain.
    """

    def test_inline_dict_entry_is_tolerated_with_warning(self, tmp_path, caplog):
        (tmp_path / "se3.yaml").write_text(
            _REGISTRY_YAML + """llm_caller:
  steps:
    implement:
      - cmd: big-claude
        priority: 10
"""
        )
        with patch("se3.config.Path.home", return_value=tmp_path):
            import logging
            with caplog.at_level(logging.WARNING, logger="se3.config"):
                agents = load_step_agents(tmp_path, "implement")

        assert agents is None
        assert any(
            "inline dict entry" in rec.message for rec in caplog.records
        )

    def test_mixed_inline_dict_and_name(self, tmp_path, caplog):
        # Inline dict skipped + warned; valid name survives.
        (tmp_path / "se3.yaml").write_text(
            _REGISTRY_YAML + """llm_caller:
  steps:
    implement:
      - opus
      - cmd: big-claude
"""
        )
        with patch("se3.config.Path.home", return_value=tmp_path):
            import logging
            with caplog.at_level(logging.WARNING, logger="se3.config"):
                agents = load_step_agents(tmp_path, "implement")

        assert agents is not None
        assert len(agents) == 1
        assert agents[0]["name"] == "opus"
        assert any(
            "inline dict entry" in rec.message for rec in caplog.records
        )


class TestUnknownAgentNameFailsFast:
    def test_unknown_name_raises_value_error(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            _REGISTRY_YAML + """llm_caller:
  steps:
    implement: [primary, doesnotexist]
"""
        )
        with patch("se3.config.Path.home", return_value=tmp_path):
            with pytest.raises(ValueError) as exc_info:
                load_step_agents(tmp_path, "implement")

        msg = str(exc_info.value)
        assert "llm_caller.steps.implement" in msg
        assert "doesnotexist" in msg
        # Available agent names should be listed sorted.
        for name in ("backup", "opus", "primary", "small"):
            assert name in msg


class TestOtherStepsUnaffected:
    def test_other_steps_return_none(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            _REGISTRY_YAML + """llm_caller:
  steps:
    implement: [opus]
"""
        )
        with patch("se3.config.Path.home", return_value=tmp_path):
            # Only 'implement' is declared.
            assert load_step_agents(tmp_path, "implement") is not None
            assert load_step_agents(tmp_path, "plan") is None
            assert load_step_agents(tmp_path, "analyze") is None
            assert load_step_agents(tmp_path, "summarize") is None


class TestMalformedTopLevelLlmCaller:
    """Top-level ``llm_caller`` must be a mapping. A scalar or list at that
    position is a yaml-structure typo; the loader must warn and fall back
    rather than crash with AttributeError during every LLMCaller
    construction.
    """

    def test_string_llm_caller_returns_none_with_warning(self, tmp_path, caplog):
        (tmp_path / "se3.yaml").write_text("llm_caller: claude\n")
        with patch("se3.config.Path.home", return_value=tmp_path):
            import logging
            with caplog.at_level(logging.WARNING, logger="se3.config"):
                agents = load_step_agents(tmp_path, "implement")

        assert agents is None
        assert any(
            "'llm_caller' is not a mapping" in rec.message
            for rec in caplog.records
        )

    def test_list_llm_caller_returns_none_with_warning(self, tmp_path, caplog):
        (tmp_path / "se3.yaml").write_text("llm_caller:\n  - cmd: foo\n")
        with patch("se3.config.Path.home", return_value=tmp_path):
            import logging
            with caplog.at_level(logging.WARNING, logger="se3.config"):
                agents = load_step_agents(tmp_path, "implement")

        assert agents is None
        assert any(
            "'llm_caller' is not a mapping" in rec.message
            for rec in caplog.records
        )

    def test_resolve_agents_falls_back_on_malformed_llm_caller(
        self, tmp_path, caplog,
    ):
        # Even with malformed llm_caller at top level, resolve_agents
        # must not raise; it should return the default chain and set the
        # override flag to False.
        (tmp_path / "se3.yaml").write_text(
            "llm_caller: claude\n"
            "claude_commands:\n"
            "  - cmd: my-claude\n"
            "    priority: 5\n"
        )
        with patch("se3.config.Path.home", return_value=tmp_path):
            import logging
            from se3.config import resolve_agents
            with caplog.at_level(logging.WARNING, logger="se3.config"):
                agents, is_override = resolve_agents(tmp_path, "implement")

        assert is_override is False
        # Default chain derived from the legacy claude_commands entry.
        assert len(agents) == 1
        assert agents[0]["cmd"] == "my-claude"
        assert any(
            "'llm_caller' is not a mapping" in rec.message
            for rec in caplog.records
        )


class TestUnknownStepKey:
    def test_typo_step_key_warns_and_is_noop(self, tmp_path, caplog):
        # User misconfigures 'inplement' (typo). Looking up any known
        # step returns None (no declaration), and a warning is logged so
        # the user can debug their yaml rather than silently get the
        # default chain.
        (tmp_path / "se3.yaml").write_text(
            _REGISTRY_YAML + """llm_caller:
  steps:
    inplement: [opus]
"""
        )
        with patch("se3.config.Path.home", return_value=tmp_path):
            import logging
            with caplog.at_level(logging.WARNING, logger="se3.config"):
                # Looking up the correctly-spelled 'implement' returns
                # None — the typo'd key does not satisfy the lookup.
                result = load_step_agents(tmp_path, "implement")

        assert result is None
        assert any(
            "unknown step key" in rec.message and "inplement" in rec.message
            for rec in caplog.records
        )

    def test_unknown_step_type_lookup_returns_none(self, tmp_path):
        # A non-empty, unknown step_type argument (not declared anywhere)
        # silently falls back to None — same as the "no declaration"
        # path. This codifies that LLMCaller's unknown step_type behaves
        # identically to a declared-but-different step.
        (tmp_path / "se3.yaml").write_text(
            _REGISTRY_YAML + """llm_caller:
  steps:
    implement: [opus]
"""
        )
        with patch("se3.config.Path.home", return_value=tmp_path):
            assert load_step_agents(tmp_path, "not_a_real_step") is None


# ---------------------------------------------------------------------------
# Nested self_check schema (per-pass agent chains)
# ---------------------------------------------------------------------------


class TestSelfCheckFlatSchema:
    """A flat self_check list is one chain reused for every pass."""

    def test_flat_list_single_chain_all_passes(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            _REGISTRY_YAML + """llm_caller:
  steps:
    self_check: [opus, primary]
"""
        )
        with patch("se3.config.Path.home", return_value=tmp_path):
            res = load_self_check_resolution(tmp_path)

        assert res.form == "flat"
        assert res.chain_count == 1
        names = [a["name"] for a in res.chain_for_pass(1)]
        assert names == ["opus", "primary"]
        # Pass 2 reuses the same (only) chain.
        assert [a["name"] for a in res.chain_for_pass(2)] == ["opus", "primary"]

    def test_flat_resolve_agents_is_override_for_all_passes(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            _REGISTRY_YAML + """llm_caller:
  steps:
    self_check: [opus]
"""
        )
        with patch("se3.config.Path.home", return_value=tmp_path):
            agents1, override1 = resolve_agents(
                tmp_path, "self_check", self_check_pass_index=1)
            agents2, override2 = resolve_agents(
                tmp_path, "self_check", self_check_pass_index=2)

        assert override1 and override2
        assert [a["name"] for a in agents1] == ["opus"]
        assert [a["name"] for a in agents2] == ["opus"]


class TestSelfCheckNestedSchema:
    """A nested self_check list selects a chain by 1-based pass index."""

    def test_nested_chains_per_pass(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            _REGISTRY_YAML + """llm_caller:
  steps:
    self_check:
      - [primary]
      - [opus, backup]
"""
        )
        with patch("se3.config.Path.home", return_value=tmp_path):
            res = load_self_check_resolution(tmp_path)

        assert res.form == "nested"
        assert res.chain_count == 2
        assert [a["name"] for a in res.chain_for_pass(1)] == ["primary"]
        assert [a["name"] for a in res.chain_for_pass(2)] == ["opus", "backup"]

    def test_nested_pass_beyond_count_reuses_last_chain(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            _REGISTRY_YAML + """llm_caller:
  steps:
    self_check:
      - [primary]
      - [opus, backup]
"""
        )
        with patch("se3.config.Path.home", return_value=tmp_path):
            res = load_self_check_resolution(tmp_path)
            # Pass 3 / 4 reuse the last chain.
            agents3, override3 = resolve_agents(
                tmp_path, "self_check", self_check_pass_index=3)

        assert [a["name"] for a in res.chain_for_pass(3)] == ["opus", "backup"]
        assert [a["name"] for a in res.chain_for_pass(99)] == ["opus", "backup"]
        assert override3
        assert [a["name"] for a in agents3] == ["opus", "backup"]

    def test_nested_resolve_agents_selects_per_pass(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            _REGISTRY_YAML + """llm_caller:
  steps:
    self_check:
      - [primary]
      - [opus]
"""
        )
        with patch("se3.config.Path.home", return_value=tmp_path):
            a1, o1 = resolve_agents(tmp_path, "self_check", self_check_pass_index=1)
            a2, o2 = resolve_agents(tmp_path, "self_check", self_check_pass_index=2)

        assert o1 and o2
        assert [a["name"] for a in a1] == ["primary"]
        assert [a["name"] for a in a2] == ["opus"]

    def test_nested_unknown_name_fails_fast(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            _REGISTRY_YAML + """llm_caller:
  steps:
    self_check:
      - [primary]
      - [doesnotexist]
"""
        )
        with patch("se3.config.Path.home", return_value=tmp_path):
            with pytest.raises(ValueError) as exc_info:
                load_self_check_resolution(tmp_path)
        assert "doesnotexist" in str(exc_info.value)


class TestSelfCheckMixedSchema:
    """A mixed list (strings AND sub-lists) warns and falls back to defaults."""

    def test_mixed_warns_and_falls_back(self, tmp_path, caplog):
        import logging
        (tmp_path / "se3.yaml").write_text(
            _REGISTRY_YAML + """llm_caller:
  defaults: [backup]
  steps:
    self_check:
      - primary
      - [opus]
"""
        )
        with patch("se3.config.Path.home", return_value=tmp_path):
            with caplog.at_level(logging.WARNING, logger="se3.config"):
                res = load_self_check_resolution(tmp_path)
                agents, is_override = resolve_agents(
                    tmp_path, "self_check", self_check_pass_index=1)

        assert res.form == "default"
        assert not res.is_override
        assert any("mixes" in rec.message for rec in caplog.records)
        # Falls back to llm_caller.defaults, NOT a step override.
        assert is_override is False
        assert [a["name"] for a in agents] == ["backup"]


class TestSelfCheckNestedOnlyForSelfCheck:
    """Nested lists are illegal for any step other than self_check."""

    def test_nested_for_other_step_is_no_override(self, tmp_path, caplog):
        import logging
        (tmp_path / "se3.yaml").write_text(
            _REGISTRY_YAML + """llm_caller:
  steps:
    implement:
      - [opus]
      - [primary]
"""
        )
        with patch("se3.config.Path.home", return_value=tmp_path):
            with caplog.at_level(logging.WARNING, logger="se3.config"):
                agents = load_step_agents(tmp_path, "implement")

        # Sub-list entries are non-strings → skipped → no usable override.
        assert agents is None
        assert any("non-str entry" in rec.message for rec in caplog.records)
