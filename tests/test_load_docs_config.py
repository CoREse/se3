"""Tests for DocsConfig and load_docs_config.

Covers:
- DocsConfig.from_dict / load defaults (no documentation: section)
- to_updater_config emits only non-None keys
- documentation: section keys are forwarded to DocumentationUpdater
- worktree-aware lookup is reused (se3.local.yaml shadows se3.yaml)
"""

from __future__ import annotations

from pathlib import Path

import yaml

from se3.config import DocsConfig, load_docs_config
from se3.engine.docs_updater import DocumentationUpdater


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


class TestDocsConfigFromDict:
    def test_empty_dict_is_empty_config(self):
        cfg = DocsConfig.from_dict({})
        assert cfg.to_updater_config() == {}

    def test_missing_documentation_section_is_empty(self):
        cfg = DocsConfig.from_dict({"version": {"enabled": True}})
        assert cfg.to_updater_config() == {}

    def test_non_mapping_documentation_section_is_empty(self):
        cfg = DocsConfig.from_dict({"documentation": ["not", "a", "mapping"]})
        assert cfg.to_updater_config() == {}

    def test_keys_parsed(self):
        cfg = DocsConfig.from_dict(
            {
                "documentation": {
                    "readme_badge_template": "v{{version}}",
                    "versions_entry_template": "## {{version}}\n{{changes}}\n",
                    "readme_header_template": "# Proj ({{version}})",
                }
            }
        )
        assert cfg.readme_badge_template == "v{{version}}"
        assert cfg.versions_entry_template == "## {{version}}\n{{changes}}\n"
        assert cfg.readme_header_template == "# Proj ({{version}})"

    def test_non_string_values_ignored(self):
        cfg = DocsConfig.from_dict(
            {
                "documentation": {
                    "readme_badge_template": 123,
                    "versions_entry_template": None,
                    "readme_header_template": ["x"],
                }
            }
        )
        assert cfg.to_updater_config() == {}

    def test_to_updater_config_only_non_none(self):
        cfg = DocsConfig(versions_entry_template="## {{version}}\n")
        assert cfg.to_updater_config() == {
            "versions_entry_template": "## {{version}}\n"
        }


class TestLoadDocsConfig:
    def test_missing_yaml_returns_empty(self, tmp_path):
        cfg = load_docs_config(tmp_path)
        assert cfg.to_updater_config() == {}

    def test_yaml_without_documentation_section(self, tmp_path):
        _write_yaml(tmp_path / "se3.yaml", {"version": {"enabled": True}})
        cfg = load_docs_config(tmp_path)
        assert cfg.to_updater_config() == {}

    def test_yaml_with_documentation_section(self, tmp_path):
        _write_yaml(
            tmp_path / "se3.yaml",
            {
                "documentation": {
                    "readme_badge_template": "version {{version}}",
                    "versions_entry_template": "## {{version}}\n\n{{changes}}\n",
                }
            },
        )
        cfg = load_docs_config(tmp_path)
        assert cfg.to_updater_config() == {
            "readme_badge_template": "version {{version}}",
            "versions_entry_template": "## {{version}}\n\n{{changes}}\n",
        }

    def test_local_yaml_shadows_yaml(self, tmp_path):
        # Reuse the existing worktree-aware / local-override lookup: a
        # plain (non-worktree) project root prefers se3.local.yaml.
        _write_yaml(
            tmp_path / "se3.yaml",
            {"documentation": {"readme_badge_template": "from-yaml"}},
        )
        _write_yaml(
            tmp_path / "se3.local.yaml",
            {"documentation": {"readme_badge_template": "from-local"}},
        )
        cfg = load_docs_config(tmp_path)
        assert cfg.readme_badge_template == "from-local"


class TestConfigForwardsToUpdater:
    def test_documentation_keys_take_effect_in_updater(self, tmp_path):
        """documentation: keys, via to_updater_config, drive the updater."""
        _write_yaml(
            tmp_path / "se3.yaml",
            {
                "documentation": {
                    "versions_entry_template": "CUSTOM {{version}} :: {{changes}}",
                }
            },
        )
        cfg = load_docs_config(tmp_path)
        updater = DocumentationUpdater(tmp_path, config=cfg.to_updater_config())
        updater.update_versions_md("9.9.9", ["did a thing"])
        content = (tmp_path / "VERSIONS.md").read_text(encoding="utf-8")
        assert "CUSTOM 9.9.9 :: - did a thing" in content
