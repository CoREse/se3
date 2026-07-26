"""Tests for the built-in default agent chain (config.py level 4).

With no user configuration at all, se3 probes its built-in candidates
against PATH and uses every one it finds, in declared order. Covers:

- Each PATH-availability permutation of ``claude`` / ``codex``.
- Survivors are renumbered from ``priority == 0``.
- No candidate available -> ``ValueError`` naming every supported agent
  and its command.
- The probe filter applies to level 4 ONLY: an explicitly configured
  ``llm_caller.defaults`` is never filtered, even when nothing is on PATH.

Every case mocks ``tianluo.config.shutil.which``, so results never depend on
whether the host running the suite happens to have claude or codex
installed.
"""

from unittest.mock import patch

import pytest

import tianluo.config as _cfg
from tianluo.config import _builtin_default_chain, load_agents, load_claude_commands


@pytest.fixture(autouse=True)
def _reset_module_caches():
    _cfg._warned_unknown_step_keys_for.clear()
    _cfg._warned_non_dict_llm_caller_for.clear()
    _cfg._warned_list_agents_for.clear()
    _cfg._warned_claude_commands_ignored_for.clear()
    _cfg._warned_claude_commands_deprecated_for.clear()
    _cfg._warned_agent_priority_deprecated_for.clear()


def _which_only(*available: str):
    """Patch ``shutil.which`` to resolve only ``available`` command names."""
    def fake_which(cmd, *args, **kwargs):
        return f"/fake/bin/{cmd}" if cmd in available else None

    return patch("tianluo.config.shutil.which", side_effect=fake_which)


def _no_global(tmp_path):
    return patch("tianluo.config.Path.home", return_value=tmp_path)


class TestBuiltinDefaultChain:
    def test_only_claude_available(self):
        with _which_only("claude"):
            chain = _builtin_default_chain()

        assert chain == [
            {"name": "claude", "type": "claude-code", "cmd": "claude",
             "priority": 0},
        ]

    def test_only_codex_available(self):
        """A lone second candidate is renumbered to priority 0, not left at 1."""
        with _which_only("codex"):
            chain = _builtin_default_chain()

        assert chain == [
            {"name": "codex", "type": "codex", "cmd": "codex", "priority": 0},
        ]

    def test_both_available_preserves_declared_order(self):
        with _which_only("claude", "codex"):
            chain = _builtin_default_chain()

        assert [a["name"] for a in chain] == ["claude", "codex"]
        assert [a["priority"] for a in chain] == [0, 1]
        assert [a["type"] for a in chain] == ["claude-code", "codex"]

    def test_none_available_raises_listing_supported_agents(self):
        with _which_only():
            with pytest.raises(ValueError) as exc:
                _builtin_default_chain()

        message = str(exc.value)
        assert "claude (command: claude)" in message
        assert "codex (command: codex)" in message
        # The user needs both escape hatches spelled out.
        assert "Install" in message
        assert "llm_caller.defaults" in message

    def test_interactive_variant_is_not_a_builtin_candidate(self):
        """claude-interactive is opt-in (needs a PTY); never auto-selected."""
        names = [c.name for c in _cfg._BUILTIN_DEFAULT_AGENTS]
        assert "claude-interactive" not in names


class TestProbeAppliesToBuiltinBranchOnly:
    def test_no_config_falls_through_to_probed_builtin(self, tmp_path):
        """No defaults and no legacy claude_commands -> level 4, probe applies."""
        (tmp_path / "tianluo.yaml").write_text("agents: {}\n")

        with _no_global(tmp_path), _which_only("codex"):
            agents = load_agents(tmp_path)
        assert [a["name"] for a in agents] == ["codex"]

        with _no_global(tmp_path), _which_only("claude", "codex"):
            agents = load_agents(tmp_path)
        assert [a["name"] for a in agents] == ["claude", "codex"]

    def test_explicit_defaults_are_never_probe_filtered(self, tmp_path):
        """Guards the invariant: probing touches level 4 and nothing else.

        An agent the user named explicitly must be returned as written even
        when its command is nowhere on PATH — silently dropping it (or
        raising) would let se3 quietly run a different agent than the one
        the user asked for, the worst possible failure mode.
        """
        (tmp_path / "tianluo.yaml").write_text("""agents:
  mine: {cmd: some-cmd}
llm_caller:
  defaults: [mine]
""")

        with _no_global(tmp_path), _which_only():
            agents = load_agents(tmp_path)

        assert [a["name"] for a in agents] == ["mine"]
        assert agents[0]["cmd"] == "some-cmd"


class TestLegacyClaudeCommandsNeverGetANonClaudeAgent:
    """``load_claude_commands`` feeds Claude-CLI-only consumers.

    Its result is wrapped in Claude-specific argv (``-p``,
    ``--output-format stream-json``, ``--setting-sources``), so a codex
    command coming out of the *built-in* chain would spawn the wrong binary.
    """

    def test_builtin_codex_only_yields_no_claude_command(self, tmp_path):
        (tmp_path / "tianluo.yaml").write_text("agents: {}\n")

        with _no_global(tmp_path), _which_only("codex"):
            assert load_claude_commands(tmp_path) == []
            # The real chain still picks codex up; only the legacy view drops it.
            assert [a["name"] for a in load_agents(tmp_path)] == ["codex"]

    def test_builtin_both_available_yields_claude_only(self, tmp_path):
        (tmp_path / "tianluo.yaml").write_text("agents: {}\n")

        with _no_global(tmp_path), _which_only("claude", "codex"):
            commands = load_claude_commands(tmp_path)

        assert commands == [{"cmd": "claude", "priority": 0}]

    def test_explicit_codex_default_still_passes_through(self, tmp_path):
        """Level 1-3 are verbatim: a named agent must not be swallowed."""
        (tmp_path / "tianluo.yaml").write_text("""agents:
  my-codex: {type: codex, cmd: codex}
llm_caller:
  defaults: [my-codex]
""")

        with _no_global(tmp_path), _which_only():
            commands = load_claude_commands(tmp_path)

        assert commands == [{"cmd": "codex", "priority": 0}]
