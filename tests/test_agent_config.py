"""Tests for agent configuration loading (load_agents).

Covers the new dict-form ``agents`` registry + ``llm_caller.defaults``
schema, legacy ``claude_commands`` auto-migration, priority sorting,
global/project merge, and backward compatibility of
``load_claude_commands``.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

import tianluo.config as _cfg
from tianluo.config import load_agents, load_claude_commands


def _which_claude_only():
    """Pin PATH probing so the built-in chain is deterministically [claude].

    The built-in fallback probes each candidate command with shutil.which;
    without this, results would vary with the host's installed agents.
    """
    return patch(
        "tianluo.config.shutil.which",
        side_effect=lambda cmd, *a, **k: (
            "/fake/bin/claude" if cmd == "claude" else None
        ),
    )


@pytest.fixture(autouse=True)
def _reset_module_caches():
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


class TestLoadAgents:
    """Test load_agents() function."""

    def test_default_when_no_config(self, tmp_path):
        """Should return the available built-in agents when no config exists."""
        with patch("tianluo.config.Path.home", return_value=tmp_path), _which_claude_only():
            agents = load_agents(tmp_path)
        assert len(agents) == 1
        assert agents[0]["name"] == "claude"
        assert agents[0]["type"] == "claude-code"
        assert agents[0]["cmd"] == "claude"
        assert agents[0]["priority"] == 0

    def test_registry_with_explicit_defaults(self, tmp_path):
        """Parse registry + explicit llm_caller.defaults name list."""
        config = tmp_path / "tianluo.yaml"
        config.write_text("""agents:
  main-claude: {cmd: claude, priority: 10}
  backup-claude: {cmd: kclaude, priority: 5}
llm_caller:
  defaults: [main-claude, backup-claude]
""")
        with patch("tianluo.config.Path.home", return_value=tmp_path):
            agents = load_agents(tmp_path)

        assert len(agents) == 2
        assert agents[0]["name"] == "main-claude"
        assert agents[0]["type"] == "claude-code"
        assert agents[0]["cmd"] == "claude"
        assert agents[0]["priority"] == 10
        assert agents[1]["name"] == "backup-claude"
        assert agents[1]["cmd"] == "kclaude"

    def test_claude_commands_fallback(self, tmp_path):
        """Should auto-migrate claude_commands to registry + implicit defaults."""
        config = tmp_path / "tianluo.yaml"
        config.write_text("""claude_commands:
  - cmd: claude
    priority: 10
  - cmd: kclaude
    priority: 5
""")
        with patch("tianluo.config.Path.home", return_value=tmp_path):
            agents = load_agents(tmp_path)

        assert len(agents) == 2
        assert agents[0]["type"] == "claude-code"
        assert agents[0]["name"] == "claude"
        assert agents[0]["cmd"] == "claude"
        assert agents[1]["type"] == "claude-code"
        assert agents[1]["cmd"] == "kclaude"

    def test_agents_takes_priority_over_claude_commands(self, tmp_path):
        """When both agents and claude_commands exist, agents wins + warning."""
        config = tmp_path / "tianluo.yaml"
        config.write_text("""agents:
  agent-claude: {cmd: claude, priority: 10}
llm_caller:
  defaults: [agent-claude]
claude_commands:
  - cmd: legacy-claude
    priority: 5
""")
        with patch("tianluo.config.Path.home", return_value=tmp_path):
            agents = load_agents(tmp_path)

        assert len(agents) == 1
        assert agents[0]["name"] == "agent-claude"

    def test_project_overrides_global(self, tmp_path):
        """Project defaults fully replace global defaults."""
        # Global config
        global_dir = tmp_path / ".se3"
        global_dir.mkdir()
        (global_dir / "config.yaml").write_text("""agents:
  global-agent: {cmd: global-claude, priority: 10}
llm_caller:
  defaults: [global-agent]
""")
        # Project config
        (tmp_path / "tianluo.yaml").write_text("""agents:
  project-agent: {cmd: project-claude, priority: 5}
llm_caller:
  defaults: [project-agent]
""")
        with patch("tianluo.config.Path.home", return_value=tmp_path):
            agents = load_agents(tmp_path)

        assert len(agents) == 1
        assert agents[0]["name"] == "project-agent"

    def test_global_used_when_no_project(self, tmp_path):
        """Global defaults used when no project config."""
        global_dir = tmp_path / ".se3"
        global_dir.mkdir()
        (global_dir / "config.yaml").write_text("""agents:
  global-agent: {cmd: global-claude, priority: 10}
llm_caller:
  defaults: [global-agent]
""")
        with patch("tianluo.config.Path.home", return_value=tmp_path):
            agents = load_agents(tmp_path)

        assert agents[0]["name"] == "global-agent"

    def test_defaults_preserve_written_order(self, tmp_path):
        """Agents follow the written order of llm_caller.defaults; the
        deprecated priority field is ignored for ordering."""
        config = tmp_path / "tianluo.yaml"
        config.write_text("""agents:
  low: {cmd: low-claude, priority: 1}
  high: {cmd: high-claude, priority: 10}
  mid: {cmd: mid-claude, priority: 5}
llm_caller:
  defaults: [low, high, mid]
""")
        with patch("tianluo.config.Path.home", return_value=tmp_path):
            agents = load_agents(tmp_path)

        # Order is the written defaults order, NOT priority descending.
        assert [a["name"] for a in agents] == ["low", "high", "mid"]

    def test_priority_field_emits_deprecation_warning_once(self, tmp_path, caplog):
        """A source carrying agents.<name>.priority warns once (deprecated)."""
        import logging
        config = tmp_path / "tianluo.yaml"
        config.write_text("""agents:
  a: {cmd: claude, priority: 1}
  b: {cmd: kclaude, priority: 2}
llm_caller:
  defaults: [a, b]
""")
        with patch("tianluo.config.Path.home", return_value=tmp_path):
            with caplog.at_level(logging.WARNING, logger="tianluo.config"):
                load_agents(tmp_path)

        priority_warnings = [
            rec for rec in caplog.records
            if "priority" in rec.message and "deprecated" in rec.message
        ]
        # Two priority fields in one source → at most one warning.
        assert len(priority_warnings) == 1

    def test_string_entries_normalized(self, tmp_path):
        """Bare string entries in agents dict should be normalized to cmd."""
        config = tmp_path / "tianluo.yaml"
        config.write_text("""agents:
  primary: claude
  backup: kclaude
llm_caller:
  defaults: [primary, backup]
""")
        with patch("tianluo.config.Path.home", return_value=tmp_path):
            agents = load_agents(tmp_path)

        assert len(agents) == 2
        by_name = {a["name"]: a for a in agents}
        assert by_name["primary"]["cmd"] == "claude"
        assert by_name["primary"]["type"] == "claude-code"
        assert by_name["backup"]["cmd"] == "kclaude"

    def test_default_type_is_claude_code(self, tmp_path):
        """Agents without type should default to claude-code."""
        config = tmp_path / "tianluo.yaml"
        config.write_text("""agents:
  no-type: {cmd: claude}
llm_caller:
  defaults: [no-type]
""")
        with patch("tianluo.config.Path.home", return_value=tmp_path):
            agents = load_agents(tmp_path)

        assert agents[0]["type"] == "claude-code"

    def test_global_agents_field(self, tmp_path):
        """Global config with registry + defaults works."""
        global_dir = tmp_path / ".se3"
        global_dir.mkdir()
        (global_dir / "config.yaml").write_text("""agents:
  global-agent: {cmd: claude, priority: 5}
llm_caller:
  defaults: [global-agent]
""")
        with patch("tianluo.config.Path.home", return_value=tmp_path):
            agents = load_agents(None)

        assert agents[0]["name"] == "global-agent"

    def test_global_claude_commands_fallback(self, tmp_path):
        """Global config with only claude_commands falls back correctly."""
        global_dir = tmp_path / ".se3"
        global_dir.mkdir()
        (global_dir / "config.yaml").write_text("""claude_commands:
  - cmd: global-claude
    priority: 5
""")
        with patch("tianluo.config.Path.home", return_value=tmp_path):
            agents = load_agents(None)

        assert agents[0]["type"] == "claude-code"
        assert agents[0]["cmd"] == "global-claude"

    def test_registry_without_defaults_uses_built_in(self, tmp_path):
        """A registry with no explicit defaults falls back to built-ins.

        The built-in chain is probed against PATH, so which() is pinned to
        expose only claude — otherwise the result would vary with whatever
        agents the host running the suite has installed.
        """
        (tmp_path / "tianluo.yaml").write_text("""agents:
  extra: {cmd: extra-claude}
""")
        with patch("tianluo.config.Path.home", return_value=tmp_path), _which_claude_only():
            agents = load_agents(tmp_path)

        # Without explicit llm_caller.defaults and without legacy
        # claude_commands, we fall back to the built-in candidates that are
        # actually available on PATH.
        assert len(agents) == 1
        assert agents[0]["name"] == "claude"
        assert agents[0]["cmd"] == "claude"

    def test_unknown_name_in_defaults_raises(self, tmp_path):
        """Unknown agent name in llm_caller.defaults raises ValueError."""
        (tmp_path / "tianluo.yaml").write_text("""agents:
  a: {cmd: claude}
llm_caller:
  defaults: [a, doesnotexist]
""")
        with patch("tianluo.config.Path.home", return_value=tmp_path):
            with pytest.raises(ValueError) as exc_info:
                load_agents(tmp_path)
        msg = str(exc_info.value)
        assert "doesnotexist" in msg
        assert "llm_caller.defaults" in msg


class TestLoadClaudeCommandsBackwardCompat:
    """Test that load_claude_commands still works via delegation."""

    def test_returns_legacy_format(self, tmp_path):
        """load_claude_commands should return {cmd, priority} dicts."""
        config = tmp_path / "tianluo.yaml"
        config.write_text("""agents:
  test: {cmd: my-claude, priority: 10}
llm_caller:
  defaults: [test]
""")
        with patch("tianluo.config.Path.home", return_value=tmp_path):
            commands = load_claude_commands(tmp_path)

        assert len(commands) == 1
        assert commands[0]["cmd"] == "my-claude"
        assert commands[0]["priority"] == 10
        assert "type" not in commands[0]
        assert "name" not in commands[0]

    def test_default_still_works(self, tmp_path):
        """Default behavior unchanged."""
        with patch("tianluo.config.Path.home", return_value=tmp_path), _which_claude_only():
            commands = load_claude_commands(tmp_path)
        assert len(commands) == 1
        assert commands[0]["cmd"] == "claude"
        assert commands[0]["priority"] == 0
