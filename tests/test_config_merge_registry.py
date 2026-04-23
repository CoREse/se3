"""Tests for global + project config merge semantics of the new schema.

Covers three merge behaviors:

- ``agents`` registry is merged entry-level (same name → project wins;
  non-conflicting names coexist).
- ``llm_caller.defaults`` is wholesale-replaced (project wins; global is
  not appended).
- ``llm_caller.steps.<step>`` is wholesale-replaced per step.
"""

from unittest.mock import patch

import pytest

import se3.config as _cfg
from se3.config import load_agent_registry, load_agents, load_step_agents


@pytest.fixture(autouse=True)
def _reset_module_caches():
    _cfg._warned_unknown_step_keys_for.clear()
    _cfg._warned_non_dict_llm_caller_for.clear()
    _cfg._warned_list_agents_for.clear()
    _cfg._warned_claude_commands_ignored_for.clear()
    _cfg._warned_claude_commands_deprecated_for.clear()
    yield
    _cfg._warned_unknown_step_keys_for.clear()
    _cfg._warned_non_dict_llm_caller_for.clear()
    _cfg._warned_list_agents_for.clear()
    _cfg._warned_claude_commands_ignored_for.clear()
    _cfg._warned_claude_commands_deprecated_for.clear()


def _write_global(tmp_path, yaml_text):
    global_dir = tmp_path / ".se3"
    global_dir.mkdir(exist_ok=True)
    (global_dir / "config.yaml").write_text(yaml_text)


def _write_project(tmp_path, yaml_text):
    (tmp_path / "se3.yaml").write_text(yaml_text)


class TestRegistryEntryLevelMerge:
    def test_agents_merge_entry_level(self, tmp_path):
        _write_global(tmp_path, """agents:
  primary: {cmd: global-primary, priority: 1}
  shared: {cmd: global-shared, priority: 2}
""")
        _write_project(tmp_path, """agents:
  primary: {cmd: project-primary, priority: 100}
  extra: {cmd: project-extra, priority: 3}
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            registry = load_agent_registry(tmp_path)

        # Same-name entry fully overridden by project.
        assert registry["primary"].cmd == "project-primary"
        assert registry["primary"].priority == 100
        # Non-conflicting entries coexist.
        assert registry["shared"].cmd == "global-shared"
        assert registry["extra"].cmd == "project-extra"

    def test_registry_merge_preserves_non_conflicting_global_entries(self, tmp_path):
        # Project declares no 'shared' → global's 'shared' survives.
        _write_global(tmp_path, """agents:
  shared: {cmd: global-shared, priority: 10}
""")
        _write_project(tmp_path, """agents:
  only_project: {cmd: extra}
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            registry = load_agent_registry(tmp_path)
        assert set(registry) == {"shared", "only_project"}


class TestDefaultsWholesaleReplace:
    def test_project_defaults_wholesale_replace(self, tmp_path):
        # Both have defaults — project must completely replace global,
        # not append.
        _write_global(tmp_path, """agents:
  g1: {cmd: g1-claude}
  g2: {cmd: g2-claude}
llm_caller:
  defaults: [g1, g2]
""")
        _write_project(tmp_path, """agents:
  p1: {cmd: p1-claude}
llm_caller:
  defaults: [p1]
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            chain = load_agents(tmp_path)

        # Only the project's chain, in the project's order — global
        # defaults are NOT appended.
        assert [a["name"] for a in chain] == ["p1"]

    def test_global_defaults_used_when_project_omits(self, tmp_path):
        _write_global(tmp_path, """agents:
  g1: {cmd: g1-claude, priority: 5}
  g2: {cmd: g2-claude, priority: 10}
llm_caller:
  defaults: [g1, g2]
""")
        _write_project(tmp_path, """agents:
  extra: {cmd: extra-claude}
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            chain = load_agents(tmp_path)

        # Global defaults resolved against merged registry. Sorted by
        # priority descending.
        assert [a["name"] for a in chain] == ["g2", "g1"]


class TestStepOverrideWholesaleReplace:
    def test_step_override_wholesale_replace(self, tmp_path):
        _write_global(tmp_path, """agents:
  g1: {cmd: g1-claude}
  g2: {cmd: g2-claude}
llm_caller:
  steps:
    implement: [g1, g2]
""")
        _write_project(tmp_path, """agents:
  p1: {cmd: p1-claude}
llm_caller:
  steps:
    implement: [p1]
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            agents = load_step_agents(tmp_path, "implement")

        assert agents is not None
        # Project override wholesale replaces global override.
        assert [a["name"] for a in agents] == ["p1"]

    def test_step_override_per_step_independent(self, tmp_path):
        # Global declares 'plan'; project declares 'implement'. Both
        # must be honored — they're independent step keys.
        _write_global(tmp_path, """agents:
  g1: {cmd: g1-claude}
llm_caller:
  steps:
    plan: [g1]
""")
        _write_project(tmp_path, """agents:
  p1: {cmd: p1-claude}
llm_caller:
  steps:
    implement: [p1]
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            plan_agents = load_step_agents(tmp_path, "plan")
            impl_agents = load_step_agents(tmp_path, "implement")

        assert plan_agents is not None
        assert [a["name"] for a in plan_agents] == ["g1"]
        assert impl_agents is not None
        assert [a["name"] for a in impl_agents] == ["p1"]
