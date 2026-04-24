"""Tests for se3.local.yaml support.

Covers:
- ``get_project_config_path`` resolution under the four combinations of
  ``se3.yaml`` and ``se3.local.yaml`` existence.
- Per-section loaders (``VersionConfig``, ``LanguageConfig``, ``TestConfig``,
  ``ConflictResolverConfig``, ``ImplementConfig``) read from
  ``se3.local.yaml`` when present, and the local file fully replaces
  ``se3.yaml`` (no deep merge).
- ``load_confirmation_config`` source labels switch to ``se3.local.yaml``
  when local is present (observable via deprecation warning text).
- Command-layer project-root detection (``is_se3_project_root``) accepts a
  directory that only contains ``se3.local.yaml``.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import se3.config as _cfg  # noqa: E402
from se3.config import (  # noqa: E402
    ConflictResolverConfig,
    DEFAULT_MAX_FIX_ITERATIONS,
    ImplementConfig,
    LanguageConfig,
    PROJECT_CONFIG_FILENAME,
    PROJECT_LOCAL_CONFIG_FILENAME,
    StepConfig,
    VersionConfig,
    apply_step_config,
    get_max_fix_iterations,
    get_project_config_path,
    is_se3_project_root,
    load_confirmation_config,
)

# Deliberately fetched via attribute access (not a direct `import TestConfig`)
# so the symbol does not enter this module's namespace under a name starting
# with ``Test`` — which would trigger pytest's class-collection warning for
# the dataclass.
_TestConfig = _cfg.TestConfig


@pytest.fixture
def isolated_global_home(monkeypatch, tmp_path):
    """Point ``Path.home()`` at a clean temp dir so tests do not accidentally
    read the real ``~/.se3/config.yaml``.
    """
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    return fake_home


# ---------------------------------------------------------------------------
# get_project_config_path — the four existence combinations
# ---------------------------------------------------------------------------


class TestGetProjectConfigPath:
    def test_neither_exists_returns_se3_yaml(self, tmp_path):
        """When neither file exists the canonical ``se3.yaml`` path is returned."""
        result = get_project_config_path(tmp_path)
        assert result == tmp_path / PROJECT_CONFIG_FILENAME

    def test_only_se3_yaml_exists(self, tmp_path):
        (tmp_path / "se3.yaml").write_text("version:\n  enabled: true\n")
        result = get_project_config_path(tmp_path)
        assert result == tmp_path / PROJECT_CONFIG_FILENAME

    def test_only_local_exists(self, tmp_path):
        (tmp_path / "se3.local.yaml").write_text("version:\n  enabled: false\n")
        result = get_project_config_path(tmp_path)
        assert result == tmp_path / PROJECT_LOCAL_CONFIG_FILENAME

    def test_both_exist_local_wins(self, tmp_path):
        (tmp_path / "se3.yaml").write_text("version:\n  enabled: true\n")
        (tmp_path / "se3.local.yaml").write_text("version:\n  enabled: false\n")
        result = get_project_config_path(tmp_path)
        assert result == tmp_path / PROJECT_LOCAL_CONFIG_FILENAME


# ---------------------------------------------------------------------------
# Per-section loaders read from se3.local.yaml when present
# ---------------------------------------------------------------------------


class TestLoadersReadLocal:
    def test_version_config_loads_from_local(self, tmp_path):
        (tmp_path / "se3.local.yaml").write_text(
            "version:\n  enabled: false\n  bump_rules:\n    feature: major\n"
        )
        cfg = VersionConfig.load(tmp_path)
        assert cfg.enabled is False
        assert cfg.bump_rules["feature"] == "major"

    def test_language_config_loads_from_local(self, tmp_path):
        (tmp_path / "se3.local.yaml").write_text(
            "language:\n  language: zh-CN\n  spec_language: en\n"
        )
        cfg = LanguageConfig.load(tmp_path)
        assert cfg.language == "zh-CN"
        assert cfg.spec_language == "en"

    def test_test_config_loads_from_local(self, tmp_path):
        (tmp_path / "se3.local.yaml").write_text(
            "test:\n"
            "  command: 'pytest -x'\n"
            "  timeout: 600\n"
            "  timeout_multiplier: 3.0\n"
        )
        cfg = _TestConfig.load(tmp_path)
        assert cfg.command == "pytest -x"
        assert cfg.timeout == 600
        assert cfg.timeout_multiplier == 3.0

    def test_conflict_resolver_config_loads_from_local(self, tmp_path):
        (tmp_path / "se3.local.yaml").write_text(
            "conflict_resolver:\n  strategy: llm\n"
        )
        cfg = ConflictResolverConfig.load(tmp_path)
        assert cfg.strategy == "llm"

    def test_implement_config_loads_from_local(self, tmp_path):
        (tmp_path / "se3.local.yaml").write_text(
            "implement:\n  group_loc_threshold: 777\n"
        )
        cfg = ImplementConfig.load(tmp_path)
        assert cfg.group_loc_threshold == 777


# ---------------------------------------------------------------------------
# When both exist, local fully replaces se3.yaml (no deep merge)
# ---------------------------------------------------------------------------


class TestLocalReplacesSe3Yaml:
    """``se3.local.yaml`` wholesale replaces ``se3.yaml`` — values from
    ``se3.yaml`` that are absent from local MUST NOT leak through.
    """

    def test_version_bump_rules_not_merged(self, tmp_path):
        # se3.yaml sets a distinctive rule that local does not redeclare.
        # Since VersionConfig.from_dict falls back to its defaults when
        # bump_rules is empty, we make local declare a non-default rule
        # that is distinct from se3.yaml's to verify the two files are
        # not merged.
        (tmp_path / "se3.yaml").write_text(
            "version:\n"
            "  enabled: true\n"
            "  bump_rules:\n"
            "    feature: major\n"
        )
        (tmp_path / "se3.local.yaml").write_text(
            "version:\n"
            "  enabled: false\n"
            "  bump_rules:\n"
            "    feature: patch\n"
        )
        cfg = VersionConfig.load(tmp_path)
        # Values from local win, not merged with se3.yaml.
        assert cfg.enabled is False
        assert cfg.bump_rules["feature"] == "patch"

    def test_language_yaml_shadowed_completely(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            "language:\n  language: en\n  spec_language: en\n"
        )
        (tmp_path / "se3.local.yaml").write_text(
            "language:\n  language: zh-CN\n"
        )
        cfg = LanguageConfig.load(tmp_path)
        assert cfg.language == "zh-CN"
        # spec_language not declared in local ⇒ must NOT leak from se3.yaml
        assert cfg.spec_language is None

    def test_implement_yaml_shadowed_when_local_missing_section(self, tmp_path):
        """When local omits a section entirely, the section defaults —
        se3.yaml values must not leak through.
        """
        (tmp_path / "se3.yaml").write_text(
            "implement:\n  group_loc_threshold: 999\n"
        )
        (tmp_path / "se3.local.yaml").write_text(
            "version:\n  enabled: true\n"
        )
        cfg = ImplementConfig.load(tmp_path)
        assert cfg.group_loc_threshold == 300  # built-in default, not 999

    def test_conflict_resolver_not_merged(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            "conflict_resolver:\n  strategy: llm\n"
        )
        (tmp_path / "se3.local.yaml").write_text(
            "conflict_resolver:\n  strategy: human\n"
        )
        cfg = ConflictResolverConfig.load(tmp_path)
        assert cfg.strategy == "human"


# ---------------------------------------------------------------------------
# Only se3.yaml present — regression: behaviour unchanged
# ---------------------------------------------------------------------------


class TestOnlySe3YamlRegression:
    def test_version_config_from_se3_yaml(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            "version:\n  enabled: false\n  bump_rules:\n    feature: patch\n"
        )
        cfg = VersionConfig.load(tmp_path)
        assert cfg.enabled is False
        assert cfg.bump_rules["feature"] == "patch"

    def test_test_config_from_se3_yaml(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            "test:\n  timeout: 1234\n"
        )
        cfg = _TestConfig.load(tmp_path)
        assert cfg.timeout == 1234


# ---------------------------------------------------------------------------
# load_confirmation_config — deprecation source_label reflects actual file
# ---------------------------------------------------------------------------


class TestConfirmationSourceLabel:
    def test_source_label_is_local_when_local_exists(
        self, tmp_path, isolated_global_home, caplog,
    ):
        """Deprecation warnings for legacy confirmation keys must name the
        file actually read. When ``se3.local.yaml`` is present it is the
        source — the warning message must mention ``se3.local.yaml``, not
        ``se3.yaml``.
        """
        (tmp_path / "se3.yaml").write_text(
            "confirmation:\n"
            "  steps:\n"
            "    plan: {reviewer: human}\n"
        )
        (tmp_path / "se3.local.yaml").write_text(
            "confirmation:\n"
            "  enabled: false\n"  # deprecated key — triggers a warning
            "  steps:\n"
            "    design: {reviewer: human}\n"
        )

        with caplog.at_level(logging.WARNING, logger="se3.config"):
            result = load_confirmation_config(tmp_path)

        # Local fully replaces yaml: only 'design' remains, not 'plan'.
        assert set(result["steps"].keys()) == {"design"}
        messages = [r.getMessage() for r in caplog.records]
        # The deprecation warning must reference the file that actually
        # contained the deprecated field.
        assert any(
            PROJECT_LOCAL_CONFIG_FILENAME in msg
            and "confirmation.enabled" in msg
            for msg in messages
        ), (
            f"expected deprecation warning to name {PROJECT_LOCAL_CONFIG_FILENAME!r}; "
            f"got: {messages}"
        )
        # And it must NOT mislabel the source as se3.yaml.
        assert not any(
            msg.startswith(PROJECT_CONFIG_FILENAME + ":")
            and "confirmation.enabled" in msg
            for msg in messages
        )

    def test_source_label_is_yaml_when_only_yaml_exists(
        self, tmp_path, isolated_global_home, caplog,
    ):
        (tmp_path / "se3.yaml").write_text(
            "confirmation:\n"
            "  enabled: false\n"
            "  steps:\n"
            "    plan: {reviewer: human}\n"
        )
        with caplog.at_level(logging.WARNING, logger="se3.config"):
            load_confirmation_config(tmp_path)

        messages = [r.getMessage() for r in caplog.records]
        assert any(
            PROJECT_CONFIG_FILENAME in msg and "confirmation.enabled" in msg
            for msg in messages
        )


# ---------------------------------------------------------------------------
# Command-layer project-root detection
# ---------------------------------------------------------------------------


class TestIsSe3ProjectRootLocalOnly:
    def test_detects_project_with_only_local(self, tmp_path):
        """A directory containing only ``se3.local.yaml`` is recognised as
        an SE3 project root. This matches the parent-walk behaviour used
        by ``se3 run`` / ``se3 issue`` / ``se3 history`` / ``se3 salvage``.
        """
        (tmp_path / "se3.local.yaml").write_text("version:\n  enabled: true\n")
        assert is_se3_project_root(tmp_path) is True

    def test_detects_project_with_only_yaml(self, tmp_path):
        (tmp_path / "se3.yaml").write_text("version:\n  enabled: true\n")
        assert is_se3_project_root(tmp_path) is True

    def test_rejects_directory_without_markers(self, tmp_path):
        assert is_se3_project_root(tmp_path) is False


class TestParentWalkFindsLocalOnlyRoot:
    """Integration coverage for the command-layer parent-walk helpers.

    ``is_se3_project_root`` is already covered in ``TestIsSe3ProjectRootLocalOnly``
    for the direct (cwd == project root) case. These tests exercise the
    parent-walk path used by ``se3 run`` / ``se3 salvage``: cwd is a
    nested subdirectory and the nearest ancestor with any SE3 marker has
    only ``se3.local.yaml``.
    """

    def test_salvage_find_project_root_walks_up_to_local_only_parent(
        self, tmp_path, monkeypatch,
    ):
        from se3.commands.salvage_cmd import _find_project_root

        # Project root has only se3.local.yaml; cwd is a nested subdir.
        project = tmp_path / "proj"
        project.mkdir()
        (project / "se3.local.yaml").write_text("version:\n  enabled: true\n")
        subdir = project / "src" / "deep"
        subdir.mkdir(parents=True)

        monkeypatch.chdir(subdir)
        assert _find_project_root() == project

    def test_run_get_project_root_walks_up_to_local_only_parent(
        self, tmp_path, monkeypatch,
    ):
        from se3.commands.run import get_project_root

        project = tmp_path / "proj"
        project.mkdir()
        (project / "se3.local.yaml").write_text("version:\n  enabled: true\n")
        subdir = project / "src" / "deep"
        subdir.mkdir(parents=True)

        monkeypatch.chdir(subdir)
        assert get_project_root() == project


# ---------------------------------------------------------------------------
# StepConfig / apply_step_config — also reads se3.local.yaml
# ---------------------------------------------------------------------------


class TestStepConfigLocal:
    def test_step_config_loads_append_from_local(self, tmp_path):
        (tmp_path / "se3.local.yaml").write_text(
            "steps:\n  append:\n    - summarize\n"
        )
        cfg = StepConfig.load(tmp_path)
        assert cfg.append_steps == ["summarize"]

    def test_step_config_local_replaces_yaml(self, tmp_path):
        """Local must fully replace yaml — yaml's append entry must not leak."""
        (tmp_path / "se3.yaml").write_text(
            "steps:\n  append:\n    - commit\n"
        )
        (tmp_path / "se3.local.yaml").write_text(
            "steps:\n  append:\n    - summarize\n"
        )
        cfg = StepConfig.load(tmp_path)
        assert cfg.append_steps == ["summarize"]

    def test_apply_step_config_uses_local(self, tmp_path):
        """``apply_step_config`` is the user-reachable entrypoint — it must
        also read from ``se3.local.yaml`` when present.
        """
        from se3.engine.models import StepType

        (tmp_path / "se3.local.yaml").write_text(
            "steps:\n  append:\n    - summarize\n"
        )
        result = apply_step_config([StepType.PLAN], project_root=tmp_path)
        # SUMMARIZE should have been appended via the local config.
        assert StepType.SUMMARIZE in result
        assert StepType.PLAN in result


# ---------------------------------------------------------------------------
# Workflow max_fix_iterations — config + verify_spec helpers
# ---------------------------------------------------------------------------


class TestMaxFixIterationsLocal:
    def test_get_max_fix_iterations_reads_local(self, tmp_path):
        (tmp_path / "se3.local.yaml").write_text(
            "workflow:\n  max_fix_iterations: 7\n"
        )
        assert get_max_fix_iterations(tmp_path) == 7

    def test_get_max_fix_iterations_local_replaces_yaml(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            "workflow:\n  max_fix_iterations: 99\n"
        )
        (tmp_path / "se3.local.yaml").write_text(
            "workflow:\n  max_fix_iterations: 7\n"
        )
        # local wins — yaml's 99 must NOT leak through.
        assert get_max_fix_iterations(tmp_path) == 7

    def test_verify_spec_get_max_fix_iterations_reads_local(self, tmp_path):
        """``verify_spec._get_max_fix_iterations`` is the per-step helper
        used inside the fix loop — it has its own get_project_config_path
        call site that must also pick up ``se3.local.yaml``.
        """
        from types import SimpleNamespace

        from se3.engine.steps.verify_spec import _get_max_fix_iterations

        (tmp_path / "se3.local.yaml").write_text(
            "workflow:\n  max_fix_iterations: 11\n"
        )
        # Build a minimal flow stub with project_root in the context so
        # the helper picks the right directory.
        flow = SimpleNamespace(
            state=SimpleNamespace(context={"project_root": str(tmp_path)}),
            change_path=None,
        )
        assert _get_max_fix_iterations(flow) == 11


# ---------------------------------------------------------------------------
# context_builder injection whitelists honour se3.local.yaml
# ---------------------------------------------------------------------------


class TestContextBuilderLocal:
    def test_issue_discovery_injection_reads_local(self, tmp_path):
        from se3.engine.context_builder import (
            ISSUE_DISCOVERY_DEFAULT_STEPS,
            get_issue_discovery_injection,
        )

        # Default behaviour: 'plan' is NOT in the default issue_discovery
        # whitelist, so the baseline (no config) returns empty.
        assert "plan" not in ISSUE_DISCOVERY_DEFAULT_STEPS
        assert get_issue_discovery_injection("plan", tmp_path) == ""

        # Adding 'plan' to issue_discovery.steps in se3.local.yaml MUST
        # take effect — proving the loader reads the local file.
        (tmp_path / "se3.local.yaml").write_text(
            "issue_discovery:\n  steps:\n    - plan\n"
        )
        injection = get_issue_discovery_injection("plan", tmp_path)
        assert injection != ""

    def test_spec_names_injection_reads_local(self, tmp_path):
        from se3.engine.context_builder import (
            SPEC_NAMES_INJECTION_DEFAULT_STEPS,
            get_spec_names_injection,
        )

        # Set up a specs/ dir so the function has something to enumerate.
        spec_dir = tmp_path / "se3" / "specs" / "alpha"
        spec_dir.mkdir(parents=True)
        (spec_dir / "spec.md").write_text("# alpha")

        # 'commit' is NOT a default whitelist member.
        assert "commit" not in SPEC_NAMES_INJECTION_DEFAULT_STEPS
        # commit is in the FORBIDDEN set, so even with an override it is
        # short-circuited; pick a step that is neither default nor
        # forbidden — 'analyze' fits.
        from se3.engine.context_builder import SPEC_NAMES_INJECTION_FORBIDDEN_STEPS
        assert "analyze" not in SPEC_NAMES_INJECTION_FORBIDDEN_STEPS
        assert "analyze" not in SPEC_NAMES_INJECTION_DEFAULT_STEPS

        # Without config, 'analyze' is not in the whitelist → empty.
        assert get_spec_names_injection("analyze", tmp_path) == ""

        # With se3.local.yaml override, 'analyze' is whitelisted.
        (tmp_path / "se3.local.yaml").write_text(
            "spec_names_injection:\n  steps:\n    - analyze\n"
        )
        injection = get_spec_names_injection("analyze", tmp_path)
        assert injection != ""


# ---------------------------------------------------------------------------
# Malformed se3.local.yaml — loaders fall back to defaults AND log a warning
# ---------------------------------------------------------------------------


class TestMalformedLocalYamlWarnings:
    """A broken ``se3.local.yaml`` silently shadows the committed
    ``se3.yaml`` and forces every loader back to built-in defaults. The
    loaders must therefore make this visible by logging a warning, so the
    user can find the typo instead of wondering why their project config
    stopped taking effect.
    """

    def _reset_warned_set(self):
        # Reset the one-shot warning dedup set so each test sees the warning.
        _cfg._warned_malformed_local_for.clear()

    def test_loader_logs_when_local_is_malformed_yaml(self, tmp_path, caplog):
        self._reset_warned_set()
        (tmp_path / "se3.yaml").write_text(
            "version:\n  enabled: false\n"
        )
        # Deliberately broken YAML: an unterminated mapping value.
        (tmp_path / "se3.local.yaml").write_text("version: {unterminated")

        with caplog.at_level(logging.WARNING, logger="se3.config"):
            cfg = VersionConfig.load(tmp_path)

        # Defaults — yaml's value (False) must NOT leak through.
        assert cfg.enabled is True
        messages = [r.getMessage() for r in caplog.records]
        # The malformed-local warning fires.
        assert any(
            PROJECT_LOCAL_CONFIG_FILENAME in msg and "shadowing" in msg
            for msg in messages
        ), f"expected local-shadow warning; got: {messages}"

    def test_loader_logs_when_local_is_non_mapping(self, tmp_path, caplog):
        self._reset_warned_set()
        (tmp_path / "se3.yaml").write_text(
            "implement:\n  group_loc_threshold: 999\n"
        )
        # A top-level list, not a mapping.
        (tmp_path / "se3.local.yaml").write_text("- one\n- two\n")

        with caplog.at_level(logging.WARNING, logger="se3.config"):
            cfg = ImplementConfig.load(tmp_path)

        assert cfg.group_loc_threshold == 300  # built-in default
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            PROJECT_LOCAL_CONFIG_FILENAME in msg and "shadowing" in msg
            for msg in messages
        ), f"expected local-shadow warning; got: {messages}"

    def test_load_agent_registry_logs_when_local_is_falsy_non_mapping(
        self, tmp_path, isolated_global_home, caplog,
    ):
        """Regression guard for the ``_read_yaml`` path.

        ``VersionConfig.load`` / ``ImplementConfig.load`` route through
        ``_load_project_yaml``; agent / llm_caller / confirmation loaders
        route through ``_read_yaml`` via ``_load_agent_configs``. Both
        paths must classify a falsy-non-mapping top-level (``[]``, ``0``,
        ``''``) as malformed — otherwise a broken ``se3.local.yaml`` would
        silently let the committed ``se3.yaml``'s agents leak through.
        """
        self._reset_warned_set()
        (tmp_path / "se3.yaml").write_text(
            "agents:\n"
            "  leaked: {type: claude-code, cmd: claude-leaked}\n"
        )
        # Top-level empty list is falsy and non-mapping — the regression
        # the old ``data or {}`` guard used to miss.
        (tmp_path / "se3.local.yaml").write_text("[]\n")

        from se3.config import load_agent_registry

        with caplog.at_level(logging.WARNING, logger="se3.config"):
            registry = load_agent_registry(tmp_path)

        # se3.yaml's agent MUST NOT leak through — registry falls back to
        # whatever the default chain produces (empty in this isolated home).
        assert "leaked" not in registry
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            PROJECT_LOCAL_CONFIG_FILENAME in msg and "shadowing" in msg
            for msg in messages
        ), f"expected local-shadow warning via _read_yaml path; got: {messages}"

    def test_valid_empty_local_yaml_no_warning(
        self, tmp_path, isolated_global_home, caplog,
    ):
        """A valid-but-empty ``se3.local.yaml`` (``{}\\n``) is NOT malformed.

        Both read paths — ``_load_project_yaml`` (per-section loaders) and
        ``_read_yaml`` via ``_load_agent_configs`` — must treat it as
        "no overrides" with zero warnings. And crucially, ``se3.yaml``
        values MUST NOT leak through: local fully replaces yaml, even
        when local is empty.
        """
        self._reset_warned_set()
        (tmp_path / "se3.yaml").write_text(
            "version:\n  enabled: false\n"
            "agents:\n"
            "  leaked: {type: claude-code, cmd: claude-leaked}\n"
        )
        (tmp_path / "se3.local.yaml").write_text("{}\n")

        from se3.config import load_agent_registry

        with caplog.at_level(logging.WARNING, logger="se3.config"):
            version_cfg = VersionConfig.load(tmp_path)
            registry = load_agent_registry(tmp_path)

        # Built-in defaults apply — yaml's False does NOT leak through.
        assert version_cfg.enabled is True
        # yaml's agent does NOT leak through either.
        assert "leaked" not in registry
        # No shadow warning: the file is valid, just empty.
        messages = [r.getMessage() for r in caplog.records]
        assert not any(
            "shadowing" in msg and PROJECT_LOCAL_CONFIG_FILENAME in msg
            for msg in messages
        ), f"no shadow warning expected for empty-but-valid local; got: {messages}"

    def test_warning_dedup_only_once_per_path(self, tmp_path, caplog):
        self._reset_warned_set()
        (tmp_path / "se3.local.yaml").write_text("version: {unterminated")

        with caplog.at_level(logging.WARNING, logger="se3.config"):
            VersionConfig.load(tmp_path)
            VersionConfig.load(tmp_path)
            ImplementConfig.load(tmp_path)

        shadow_warnings = [
            r for r in caplog.records
            if "shadowing" in r.getMessage()
            and PROJECT_LOCAL_CONFIG_FILENAME in r.getMessage()
        ]
        assert len(shadow_warnings) == 1, (
            f"local-shadow warning must dedupe per path; "
            f"got {len(shadow_warnings)}: {[r.getMessage() for r in shadow_warnings]}"
        )


# ---------------------------------------------------------------------------
# Non-file se3.local.yaml (directory / dangling symlink) — get_project_config_path
# must NOT treat them as the active config
# ---------------------------------------------------------------------------


class TestGetProjectConfigPathNonFile:
    def test_directory_at_local_path_falls_back_to_yaml(self, tmp_path):
        """An ``se3.local.yaml`` directory (created by mistake) must not
        shadow the committed ``se3.yaml`` — a directory cannot be parsed
        as a config file, so falling back keeps the project usable.
        """
        (tmp_path / "se3.yaml").write_text("version:\n  enabled: true\n")
        (tmp_path / PROJECT_LOCAL_CONFIG_FILENAME).mkdir()
        result = get_project_config_path(tmp_path)
        assert result == tmp_path / PROJECT_CONFIG_FILENAME

    def test_symlink_to_real_file_is_accepted_as_override(self, tmp_path):
        """A symlink at ``se3.local.yaml`` pointing to a regular file is
        treated as a valid override. ``is_file()`` follows symlinks, so
        the common "share local overrides between clones via a symlink"
        workflow is supported. The loader must therefore read values
        from the symlink target, and ``se3.yaml`` must NOT leak through.
        """
        shared = tmp_path / "shared-overrides.yaml"
        shared.write_text("version:\n  enabled: false\n")
        (tmp_path / "se3.yaml").write_text("version:\n  enabled: true\n")
        link = tmp_path / PROJECT_LOCAL_CONFIG_FILENAME
        link.symlink_to(shared)

        # The symlinked path is selected as the active config.
        assert get_project_config_path(tmp_path) == link
        # And its contents drive the loaded config — yaml's True does
        # NOT leak through.
        cfg = VersionConfig.load(tmp_path)
        assert cfg.enabled is False

    def test_dangling_symlink_at_local_path_falls_back_to_yaml(self, tmp_path):
        """A dangling symlink at ``se3.local.yaml`` is not a real file —
        ``get_project_config_path`` must skip it rather than return a
        path that downstream readers will fail to open.
        """
        (tmp_path / "se3.yaml").write_text("version:\n  enabled: true\n")
        link = tmp_path / PROJECT_LOCAL_CONFIG_FILENAME
        link.symlink_to(tmp_path / "nonexistent.yaml")
        result = get_project_config_path(tmp_path)
        assert result == tmp_path / PROJECT_CONFIG_FILENAME

    @pytest.mark.skipif(
        sys.platform.startswith("win") or os.geteuid() == 0,
        reason="chmod 0 is not honored on Windows or when running as root",
    )
    def test_unreadable_regular_file_still_selected_and_readers_fall_back(
        self, tmp_path, caplog,
    ):
        """An ``se3.local.yaml`` that exists as a regular file but is not
        readable (e.g. ``chmod 0o000``) is still selected by
        ``get_project_config_path`` — ``is_file()`` returns True and the
        helper deliberately does not probe read permission. Downstream
        ``_read_yaml`` then fails to open it with an ``OSError`` and the
        loaders fall back to built-in defaults with a shadow warning.
        """
        _cfg._warned_malformed_local_for.clear()

        (tmp_path / "se3.yaml").write_text(
            "version:\n  enabled: false\n"
        )
        local = tmp_path / PROJECT_LOCAL_CONFIG_FILENAME
        local.write_text("version:\n  enabled: false\n")
        try:
            os.chmod(local, 0)

            # Sanity-check: is_file() remains True even when unreadable —
            # file existence and read permission are orthogonal on POSIX.
            assert local.is_file()
            # get_project_config_path still routes to the local file.
            assert get_project_config_path(tmp_path) == local

            # The loader swallows the OSError, logs a warning, and falls
            # back to built-in defaults — yaml's False must NOT leak.
            with caplog.at_level(logging.WARNING, logger="se3.config"):
                cfg = VersionConfig.load(tmp_path)

            assert cfg.enabled is True  # built-in default
            messages = [r.getMessage() for r in caplog.records]
            # The generic "failed to read" warning (OSError path) AND the
            # local-shadow warning both fire.
            assert any(
                "failed to read" in msg and PROJECT_LOCAL_CONFIG_FILENAME in msg
                for msg in messages
            ), f"expected 'failed to read' OSError warning; got: {messages}"
            assert any(
                "shadowing" in msg and PROJECT_LOCAL_CONFIG_FILENAME in msg
                for msg in messages
            ), f"expected local-shadow warning; got: {messages}"
        finally:
            # Restore permissions so tmp_path cleanup can unlink the file.
            os.chmod(local, stat.S_IRUSR | stat.S_IWUSR)


# ---------------------------------------------------------------------------
# Global ~/.se3/config.yaml + se3.local.yaml — entry-level merge through
# _load_agent_configs / load_agents
# ---------------------------------------------------------------------------


class TestGlobalPlusLocalAgentMerge:
    """Verify that ``~/.se3/config.yaml`` and ``se3.local.yaml`` combine
    through the same merge rules that apply to ``se3.yaml`` — local is a
    drop-in replacement for ``se3.yaml`` at the project layer, not a
    bypass of the global layer.
    """

    def test_agents_merge_entry_level_global_plus_local(
        self, tmp_path, isolated_global_home,
    ):
        """Global declares ``primary``; local declares ``backup``. The
        merged registry must contain both — local does NOT erase global's
        agents the way it erases ``se3.yaml``'s.
        """
        from se3.config import load_agent_registry

        global_cfg = isolated_global_home / ".se3" / "config.yaml"
        global_cfg.parent.mkdir(parents=True, exist_ok=True)
        global_cfg.write_text(
            "agents:\n"
            "  primary: {type: claude-code, cmd: claude, priority: 10}\n"
        )
        (tmp_path / "se3.local.yaml").write_text(
            "agents:\n"
            "  backup: {type: claude-code, cmd: claude-dev, priority: 5}\n"
        )

        registry = load_agent_registry(tmp_path)
        assert set(registry.keys()) == {"primary", "backup"}
        assert registry["primary"].cmd == "claude"
        assert registry["backup"].cmd == "claude-dev"

    def test_local_overrides_global_for_same_agent_name(
        self, tmp_path, isolated_global_home,
    ):
        """When global and local declare the same agent name, local wins
        (entry-level merge), exactly mirroring se3.yaml's behaviour.
        """
        from se3.config import load_agent_registry

        global_cfg = isolated_global_home / ".se3" / "config.yaml"
        global_cfg.parent.mkdir(parents=True, exist_ok=True)
        global_cfg.write_text(
            "agents:\n"
            "  primary: {type: claude-code, cmd: claude-from-global}\n"
        )
        (tmp_path / "se3.local.yaml").write_text(
            "agents:\n"
            "  primary: {type: claude-code, cmd: claude-from-local}\n"
        )

        registry = load_agent_registry(tmp_path)
        assert registry["primary"].cmd == "claude-from-local"

    def test_load_agents_uses_global_defaults_when_local_omits_them(
        self, tmp_path, isolated_global_home,
    ):
        """``llm_caller.defaults`` whole-replace at the project layer is
        relative to ``se3.local.yaml`` (when present). When local omits
        defaults, global defaults still apply — local does not
        accidentally null out the global chain.
        """
        from se3.config import load_agents

        global_cfg = isolated_global_home / ".se3" / "config.yaml"
        global_cfg.parent.mkdir(parents=True, exist_ok=True)
        global_cfg.write_text(
            "agents:\n"
            "  primary: {type: claude-code, cmd: claude, priority: 10}\n"
            "  backup:  {type: claude-code, cmd: claude-dev, priority: 5}\n"
            "llm_caller:\n"
            "  defaults: [primary, backup]\n"
        )
        # Local declares no llm_caller section at all, only adds an extra
        # agent — the global default chain must remain in effect.
        (tmp_path / "se3.local.yaml").write_text(
            "agents:\n"
            "  opus: {type: claude-code, cmd: claude-opus, priority: 20}\n"
        )

        agents = load_agents(tmp_path)
        names = [a["name"] for a in agents]
        assert names == ["primary", "backup"]

    def test_local_llm_caller_defaults_replace_global(
        self, tmp_path, isolated_global_home,
    ):
        """When local DOES declare ``llm_caller.defaults``, it whole-
        replaces the global list (no append) — same semantics as
        se3.yaml vs global.
        """
        from se3.config import load_agents

        global_cfg = isolated_global_home / ".se3" / "config.yaml"
        global_cfg.parent.mkdir(parents=True, exist_ok=True)
        global_cfg.write_text(
            "agents:\n"
            "  primary: {type: claude-code, cmd: claude, priority: 10}\n"
            "llm_caller:\n"
            "  defaults: [primary]\n"
        )
        (tmp_path / "se3.local.yaml").write_text(
            "agents:\n"
            "  opus: {type: claude-code, cmd: claude-opus, priority: 20}\n"
            "llm_caller:\n"
            "  defaults: [opus]\n"
        )

        agents = load_agents(tmp_path)
        names = [a["name"] for a in agents]
        assert names == ["opus"]
