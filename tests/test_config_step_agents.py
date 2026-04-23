"""Tests for per-step agent override loading (load_step_agents).

Covers:
- Missing declaration returns None (fall back to default chain).
- Legal declaration returns normalized+sorted list.
- Project-level declaration fully replaces global declaration.
- Empty list returns None + warning.
- Structurally invalid values return None + warning.
- A declaration for one step does not affect other (unaffected) steps.
"""

from unittest.mock import patch

import pytest

import se3.config as _cfg
from se3.config import load_step_agents


@pytest.fixture(autouse=True)
def _reset_unknown_step_key_dedup_cache():
    """Clear the module-level typo-warning dedup cache before each test.

    Without this, the first test to log a typo warning for a given
    ``(source_label, unknown_keys)`` tuple poisons every subsequent test
    that expects to observe the same warning — producing an
    ordering-dependent false pass.
    """
    _cfg._warned_unknown_step_keys_for.clear()
    _cfg._warned_non_dict_llm_caller_for.clear()
    yield
    _cfg._warned_unknown_step_keys_for.clear()
    _cfg._warned_non_dict_llm_caller_for.clear()


class TestLoadStepAgentsNoConfig:
    def test_returns_none_when_no_config(self, tmp_path):
        with patch("se3.config.Path.home", return_value=tmp_path):
            assert load_step_agents(tmp_path, "implement") is None

    def test_returns_none_when_step_not_declared(self, tmp_path):
        (tmp_path / "se3.yaml").write_text("""llm_caller:
  steps:
    implement:
      - cmd: big-claude
        priority: 10
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            # 'plan' is not declared; should return None even though
            # 'implement' is.
            assert load_step_agents(tmp_path, "plan") is None

    def test_returns_none_when_step_type_empty(self, tmp_path):
        with patch("se3.config.Path.home", return_value=tmp_path):
            assert load_step_agents(tmp_path, "") is None
            assert load_step_agents(tmp_path, None) is None


class TestLoadStepAgentsLegalDeclaration:
    def test_dict_entries_normalized_and_sorted(self, tmp_path):
        (tmp_path / "se3.yaml").write_text("""llm_caller:
  steps:
    implement:
      - name: low
        cmd: low-claude
        priority: 1
      - name: high
        cmd: high-claude
        priority: 10
      - name: mid
        cmd: mid-claude
        priority: 5
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            agents = load_step_agents(tmp_path, "implement")

        assert agents is not None
        assert [a["name"] for a in agents] == ["high", "mid", "low"]
        # Type defaulted to claude-code
        assert all(a["type"] == "claude-code" for a in agents)

    def test_string_entries_normalized(self, tmp_path):
        (tmp_path / "se3.yaml").write_text("""llm_caller:
  steps:
    summarize:
      - claude
      - kclaude
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            agents = load_step_agents(tmp_path, "summarize")

        assert agents is not None
        assert len(agents) == 2
        assert agents[0]["name"] == "claude"
        assert agents[0]["cmd"] == "claude"
        assert agents[0]["type"] == "claude-code"


class TestProjectOverridesGlobal:
    def test_project_replaces_global_for_same_step(self, tmp_path):
        # Global declares an override for implement.
        global_dir = tmp_path / ".se3"
        global_dir.mkdir()
        (global_dir / "config.yaml").write_text("""llm_caller:
  steps:
    implement:
      - name: global-agent
        cmd: global-claude
        priority: 10
""")
        # Project also declares an override for implement.
        (tmp_path / "se3.yaml").write_text("""llm_caller:
  steps:
    implement:
      - name: project-agent
        cmd: project-claude
        priority: 5
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            agents = load_step_agents(tmp_path, "implement")

        assert agents is not None
        assert len(agents) == 1
        assert agents[0]["name"] == "project-agent"

    def test_global_used_when_project_does_not_declare_step(self, tmp_path):
        global_dir = tmp_path / ".se3"
        global_dir.mkdir()
        (global_dir / "config.yaml").write_text("""llm_caller:
  steps:
    implement:
      - name: global-agent
        cmd: global-claude
        priority: 10
""")
        # Project declares nothing under llm_caller.
        (tmp_path / "se3.yaml").write_text("claude_commands:\n  - cmd: claude\n")
        with patch("se3.config.Path.home", return_value=tmp_path):
            agents = load_step_agents(tmp_path, "implement")

        assert agents is not None
        assert agents[0]["name"] == "global-agent"


class TestInvalidDeclarations:
    def test_empty_list_returns_none(self, tmp_path, caplog):
        (tmp_path / "se3.yaml").write_text("""llm_caller:
  steps:
    implement: []
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            import logging
            with caplog.at_level(logging.WARNING, logger="se3.config"):
                agents = load_step_agents(tmp_path, "implement")

        assert agents is None
        assert any("llm_caller.steps.implement" in rec.message
                   for rec in caplog.records)

    def test_non_list_returns_none(self, tmp_path, caplog):
        # A bare string instead of a list.
        (tmp_path / "se3.yaml").write_text("""llm_caller:
  steps:
    implement: "claude"
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            import logging
            with caplog.at_level(logging.WARNING, logger="se3.config"):
                agents = load_step_agents(tmp_path, "implement")

        assert agents is None
        assert any("not a list" in rec.message for rec in caplog.records)

    def test_entries_of_wrong_type_filtered(self, tmp_path, caplog):
        # Mixed list: one valid dict, one integer (junk).
        (tmp_path / "se3.yaml").write_text("""llm_caller:
  steps:
    implement:
      - name: good
        cmd: good-claude
        priority: 5
      - 42
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            import logging
            with caplog.at_level(logging.WARNING, logger="se3.config"):
                agents = load_step_agents(tmp_path, "implement")

        # Valid entry survives; junk entry is dropped with a warning.
        assert agents is not None
        assert len(agents) == 1
        assert agents[0]["name"] == "good"
        assert any("non-str/dict" in rec.message for rec in caplog.records)


class TestOtherStepsUnaffected:
    def test_other_steps_return_none(self, tmp_path):
        (tmp_path / "se3.yaml").write_text("""llm_caller:
  steps:
    implement:
      - name: big
        cmd: big-claude
        priority: 10
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            # Only 'implement' is declared.
            assert load_step_agents(tmp_path, "implement") is not None
            assert load_step_agents(tmp_path, "plan") is None
            assert load_step_agents(tmp_path, "analyze") is None
            assert load_step_agents(tmp_path, "summarize") is None


class TestMalformedDictEntries:
    def test_dict_without_cmd_is_rejected(self, tmp_path, caplog):
        # Entries missing 'cmd' must not silently default to a plain
        # 'claude' agent — the hard-override contract requires the user's
        # declared list to be exactly what runs, so typos should fail
        # loudly (warning) rather than silently succeed.
        (tmp_path / "se3.yaml").write_text("""llm_caller:
  steps:
    implement:
      - priority: 10
      - name: big
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            import logging
            with caplog.at_level(logging.WARNING, logger="se3.config"):
                agents = load_step_agents(tmp_path, "implement")

        assert agents is None
        assert any("no usable 'cmd'" in rec.message for rec in caplog.records)

    def test_mixed_valid_and_invalid_dicts(self, tmp_path, caplog):
        # A valid entry alongside an invalid one — valid survives,
        # invalid is dropped with a warning.
        (tmp_path / "se3.yaml").write_text("""llm_caller:
  steps:
    implement:
      - name: good
        cmd: good-claude
        priority: 5
      - priority: 99
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            import logging
            with caplog.at_level(logging.WARNING, logger="se3.config"):
                agents = load_step_agents(tmp_path, "implement")

        assert agents is not None
        assert len(agents) == 1
        assert agents[0]["name"] == "good"
        assert any("no usable 'cmd'" in rec.message for rec in caplog.records)


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
        # Even with malformed llm_caller at top level, resolve_agents must
        # not raise; it should return the default chain and set the
        # override flag to False.
        (tmp_path / "se3.yaml").write_text(
            "llm_caller: claude\nclaude_commands:\n  - cmd: my-claude\n    priority: 5\n"
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
        (tmp_path / "se3.yaml").write_text("""llm_caller:
  steps:
    inplement:
      - cmd: big-claude
        priority: 10
""")
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
        (tmp_path / "se3.yaml").write_text("""llm_caller:
  steps:
    implement:
      - cmd: big-claude
        priority: 10
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            assert load_step_agents(tmp_path, "not_a_real_step") is None
