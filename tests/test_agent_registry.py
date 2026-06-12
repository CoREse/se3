"""Tests for the top-level agent registry (``load_agent_registry``).

Covers:

- Dict-form top-level ``agents`` is parsed into ``{name: AgentDef}``.
- Entry-level global + project merge: project overrides by name,
  non-conflicting entries coexist.
- List-form top-level ``agents`` is warning+ignored.
- Non-dict top-level ``agents`` is warning+ignored.
- Legacy ``claude_commands`` is auto-migrated when ``agents`` is absent:
  names are slugified from ``cmd``; collisions append ``_2`` / ``_3`` …
- Legacy ``claude_commands`` is ignored with a warning when ``agents``
  is explicitly set in the same source.
- Deprecation warning on migration contains the equivalent new-schema
  YAML snippet.
- Empty registry is fine — the default chain falls back to built-in
  ``claude``.
"""

from unittest.mock import patch

import pytest
import logging

import se3.config as _cfg
from se3.config import (
    AgentDef,
    load_agent_registry,
    load_agents,
    _slugify_cmd,
    _BUILTIN_DEFAULT_AGENT_NAME,
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


def _no_global(tmp_path):
    """Context-manager style patch of Path.home() to silence global config.

    The caller typically uses ``with _no_global(tmp_path):`` to scope the
    patch tightly around the call under test.
    """
    return patch("se3.config.Path.home", return_value=tmp_path)


class TestDictForm:
    def test_basic_dict_registry(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            """agents:
  primary: {cmd: claude, priority: 10}
  backup: {cmd: claude-dev, priority: 5}
"""
        )
        with _no_global(tmp_path):
            registry = load_agent_registry(tmp_path)

        assert set(registry.keys()) == {"primary", "backup"}
        assert registry["primary"].cmd == "claude"
        assert registry["primary"].priority == 10
        assert registry["backup"].cmd == "claude-dev"
        # Default type is claude-code.
        assert registry["primary"].type == "claude-code"

    def test_registry_with_string_entry(self, tmp_path):
        # A bare string entry → treated as cmd.
        (tmp_path / "se3.yaml").write_text(
            """agents:
  primary: claude
"""
        )
        with _no_global(tmp_path):
            registry = load_agent_registry(tmp_path)
        assert registry["primary"].cmd == "claude"
        assert registry["primary"].priority == 0

    def test_entry_without_cmd_is_skipped(self, tmp_path, caplog):
        (tmp_path / "se3.yaml").write_text(
            """agents:
  bad: {priority: 10}
  good: {cmd: claude, priority: 5}
"""
        )
        with _no_global(tmp_path):
            with caplog.at_level(logging.WARNING, logger="se3.config"):
                registry = load_agent_registry(tmp_path)

        assert "bad" not in registry
        assert "good" in registry
        assert any("no usable 'cmd'" in rec.message for rec in caplog.records)

    def test_empty_registry_does_not_raise(self, tmp_path):
        (tmp_path / "se3.yaml").write_text("agents: {}\n")
        with _no_global(tmp_path):
            registry = load_agent_registry(tmp_path)
        assert registry == {}
        # load_agents falls back to built-in.
        with _no_global(tmp_path):
            chain = load_agents(tmp_path)
        assert [a["name"] for a in chain] == [_BUILTIN_DEFAULT_AGENT_NAME]


class TestEntryLevelMerge:
    def test_project_overrides_same_name(self, tmp_path):
        global_dir = tmp_path / ".se3"
        global_dir.mkdir()
        (global_dir / "config.yaml").write_text(
            """agents:
  primary: {cmd: global-claude, priority: 1}
  shared: {cmd: shared-global, priority: 5}
"""
        )
        (tmp_path / "se3.yaml").write_text(
            """agents:
  primary: {cmd: project-claude, priority: 10}
  only_project: {cmd: extra, priority: 2}
"""
        )
        with _no_global(tmp_path):
            registry = load_agent_registry(tmp_path)

        # Same name → project overrides global.
        assert registry["primary"].cmd == "project-claude"
        assert registry["primary"].priority == 10
        # Non-conflicting from global is preserved.
        assert registry["shared"].cmd == "shared-global"
        # Non-conflicting from project is preserved.
        assert registry["only_project"].cmd == "extra"


class TestListFormIgnored:
    def test_list_agents_warned_and_ignored(self, tmp_path, caplog):
        (tmp_path / "se3.yaml").write_text(
            """agents:
  - name: primary
    cmd: claude
"""
        )
        with _no_global(tmp_path):
            with caplog.at_level(logging.WARNING, logger="se3.config"):
                registry = load_agent_registry(tmp_path)

        assert registry == {}
        assert any(
            "'agents' is a list" in rec.message for rec in caplog.records
        )

    def test_scalar_agents_warned_and_ignored(self, tmp_path, caplog):
        (tmp_path / "se3.yaml").write_text("agents: claude\n")
        with _no_global(tmp_path):
            with caplog.at_level(logging.WARNING, logger="se3.config"):
                registry = load_agent_registry(tmp_path)

        assert registry == {}
        assert any(
            "not a mapping" in rec.message for rec in caplog.records
        )


class TestClaudeCommandsLegacyMigration:
    def test_legacy_migration_creates_registry_and_defaults(
        self, tmp_path, caplog,
    ):
        (tmp_path / "se3.yaml").write_text(
            """claude_commands:
  - cmd: claude
    priority: 10
  - cmd: claude-dev
    priority: 5
"""
        )
        with _no_global(tmp_path):
            with caplog.at_level(logging.WARNING, logger="se3.config"):
                registry = load_agent_registry(tmp_path)

        assert set(registry.keys()) == {"claude", "claude-dev"}
        assert registry["claude"].cmd == "claude"
        assert registry["claude-dev"].cmd == "claude-dev"

        # Deprecation warning mentioning new-schema equivalent.
        msgs = [rec.message for rec in caplog.records]
        assert any(
            "claude_commands" in m and "deprecated" in m for m in msgs
        )
        combined = "\n".join(msgs)
        assert "agents:" in combined
        assert "llm_caller:" in combined
        assert "defaults:" in combined

    def test_legacy_cmd_collision_adds_suffix(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            """claude_commands:
  - cmd: claude
  - cmd: claude
  - cmd: claude
"""
        )
        with _no_global(tmp_path):
            registry = load_agent_registry(tmp_path)

        assert set(registry.keys()) == {"claude", "claude_2", "claude_3"}

    def test_legacy_defaults_chain_feeds_load_agents(self, tmp_path):
        # When only claude_commands is present, load_agents builds the
        # chain from the migrated names in list order.
        (tmp_path / "se3.yaml").write_text(
            """claude_commands:
  - cmd: claude
    priority: 1
  - cmd: claude-dev
    priority: 10
"""
        )
        with _no_global(tmp_path):
            chain = load_agents(tmp_path)

        # Chain preserves the legacy entry order — priority is ignored.
        assert [a["name"] for a in chain] == ["claude", "claude-dev"]

    def test_claude_commands_ignored_when_agents_present(self, tmp_path, caplog):
        (tmp_path / "se3.yaml").write_text(
            """agents:
  real: {cmd: real-claude}
claude_commands:
  - cmd: ignored-claude
"""
        )
        with _no_global(tmp_path):
            with caplog.at_level(logging.WARNING, logger="se3.config"):
                registry = load_agent_registry(tmp_path)

        assert set(registry.keys()) == {"real"}
        # 'ignored-claude' is not migrated (no 'ignored-claude' key).
        assert "ignored-claude" not in registry
        assert any(
            "both 'agents' and 'claude_commands'" in rec.message
            for rec in caplog.records
        )


class TestSlugify:
    def test_preserves_alphanum_hyphen_underscore(self):
        assert _slugify_cmd("claude-dev_v2") == "claude-dev_v2"

    def test_replaces_other_chars(self):
        assert _slugify_cmd("claude cli") == "claude_cli"
        assert _slugify_cmd("claude@test") == "claude_test"

    def test_empty_fallback(self):
        assert _slugify_cmd("") == "agent"


class TestAgentDef:
    def test_to_agent_dict_shape(self):
        agent = AgentDef(name="x", type="claude-code", cmd="claude", priority=7)
        d = agent.to_agent_dict()
        assert d == {
            "name": "x",
            "type": "claude-code",
            "cmd": "claude",
            "priority": 7,
        }

    def test_defaults(self):
        agent = AgentDef(name="x", cmd="c")
        assert agent.type == "claude-code"
        assert agent.priority == 0
