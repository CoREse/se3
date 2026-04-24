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
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import se3.config as _cfg  # noqa: E402
from se3.config import (  # noqa: E402
    ConflictResolverConfig,
    ImplementConfig,
    LanguageConfig,
    PROJECT_CONFIG_FILENAME,
    PROJECT_LOCAL_CONFIG_FILENAME,
    VersionConfig,
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
