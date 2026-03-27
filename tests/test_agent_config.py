"""Tests for agent configuration loading (load_agents).

Covers new agents format, legacy claude_commands fallback,
mixed scenarios, priority sorting, and backward compatibility.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from se3.config import load_agents, load_claude_commands


class TestLoadAgents:
    """Test load_agents() function."""

    def test_default_when_no_config(self, tmp_path):
        """Should return default agent when no config exists."""
        with patch("se3.config.Path.home", return_value=tmp_path):
            agents = load_agents(tmp_path)
        assert len(agents) == 1
        assert agents[0]["name"] == "claude"
        assert agents[0]["type"] == "claude-code"
        assert agents[0]["cmd"] == "claude"
        assert agents[0]["priority"] == 0

    def test_agents_field_parsed(self, tmp_path):
        """Should parse agents field with type attribute."""
        config = tmp_path / "se3.yaml"
        config.write_text("""agents:
  - name: main-claude
    type: claude-code
    cmd: claude
    priority: 10
  - name: backup-claude
    type: claude-code
    cmd: kclaude
    priority: 5
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            agents = load_agents(tmp_path)

        assert len(agents) == 2
        assert agents[0]["name"] == "main-claude"
        assert agents[0]["type"] == "claude-code"
        assert agents[0]["cmd"] == "claude"
        assert agents[0]["priority"] == 10
        assert agents[1]["name"] == "backup-claude"
        assert agents[1]["cmd"] == "kclaude"

    def test_claude_commands_fallback(self, tmp_path):
        """Should fallback to claude_commands and auto-add type."""
        config = tmp_path / "se3.yaml"
        config.write_text("""claude_commands:
  - cmd: claude
    priority: 10
  - cmd: kclaude
    priority: 5
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            agents = load_agents(tmp_path)

        assert len(agents) == 2
        assert agents[0]["type"] == "claude-code"
        assert agents[0]["name"] == "claude"
        assert agents[0]["cmd"] == "claude"
        assert agents[1]["type"] == "claude-code"
        assert agents[1]["cmd"] == "kclaude"

    def test_agents_takes_priority_over_claude_commands(self, tmp_path):
        """When both agents and claude_commands exist, agents wins."""
        config = tmp_path / "se3.yaml"
        config.write_text("""agents:
  - name: agent-claude
    cmd: claude
    priority: 10
claude_commands:
  - cmd: legacy-claude
    priority: 5
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            agents = load_agents(tmp_path)

        assert len(agents) == 1
        assert agents[0]["name"] == "agent-claude"

    def test_project_overrides_global(self, tmp_path):
        """Project config should override global config."""
        # Global config
        global_dir = tmp_path / ".se3"
        global_dir.mkdir()
        (global_dir / "config.yaml").write_text("""agents:
  - name: global-agent
    cmd: global-claude
    priority: 10
""")
        # Project config
        (tmp_path / "se3.yaml").write_text("""agents:
  - name: project-agent
    cmd: project-claude
    priority: 5
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            agents = load_agents(tmp_path)

        assert len(agents) == 1
        assert agents[0]["name"] == "project-agent"

    def test_global_used_when_no_project(self, tmp_path):
        """Global config used when no project config."""
        global_dir = tmp_path / ".se3"
        global_dir.mkdir()
        (global_dir / "config.yaml").write_text("""agents:
  - name: global-agent
    cmd: global-claude
    priority: 10
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            agents = load_agents(tmp_path)

        assert agents[0]["name"] == "global-agent"

    def test_priority_sorting(self, tmp_path):
        """Agents should be sorted by priority descending."""
        config = tmp_path / "se3.yaml"
        config.write_text("""agents:
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
            agents = load_agents(tmp_path)

        assert [a["name"] for a in agents] == ["high", "mid", "low"]

    def test_string_agents_normalized(self, tmp_path):
        """String entries in agents should be normalized."""
        config = tmp_path / "se3.yaml"
        config.write_text("""agents:
  - claude
  - kclaude
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            agents = load_agents(tmp_path)

        assert len(agents) == 2
        assert agents[0]["name"] == "claude"
        assert agents[0]["type"] == "claude-code"
        assert agents[0]["cmd"] == "claude"

    def test_default_type_is_claude_code(self, tmp_path):
        """Agents without type should default to claude-code."""
        config = tmp_path / "se3.yaml"
        config.write_text("""agents:
  - name: no-type
    cmd: claude
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            agents = load_agents(tmp_path)

        assert agents[0]["type"] == "claude-code"

    def test_global_agents_field(self, tmp_path):
        """Global config with agents field works."""
        global_dir = tmp_path / ".se3"
        global_dir.mkdir()
        (global_dir / "config.yaml").write_text("""agents:
  - name: global-agent
    type: claude-code
    cmd: claude
    priority: 5
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
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
        with patch("se3.config.Path.home", return_value=tmp_path):
            agents = load_agents(None)

        assert agents[0]["type"] == "claude-code"
        assert agents[0]["cmd"] == "global-claude"


class TestLoadClaudeCommandsBackwardCompat:
    """Test that load_claude_commands still works via delegation."""

    def test_returns_legacy_format(self, tmp_path):
        """load_claude_commands should return {cmd, priority} dicts."""
        config = tmp_path / "se3.yaml"
        config.write_text("""agents:
  - name: test
    type: claude-code
    cmd: my-claude
    priority: 10
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            commands = load_claude_commands(tmp_path)

        assert len(commands) == 1
        assert commands[0]["cmd"] == "my-claude"
        assert commands[0]["priority"] == 10
        assert "type" not in commands[0]
        assert "name" not in commands[0]

    def test_default_still_works(self, tmp_path):
        """Default behavior unchanged."""
        with patch("se3.config.Path.home", return_value=tmp_path):
            commands = load_claude_commands(tmp_path)
        assert len(commands) == 1
        assert commands[0]["cmd"] == "claude"
        assert commands[0]["priority"] == 0
