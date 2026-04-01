"""Tests for ImplementConfig configuration."""

from __future__ import annotations

from se3.config import ImplementConfig


class TestImplementConfigDefaults:
    """Tests for ImplementConfig default values."""

    def test_default_threshold(self):
        config = ImplementConfig()
        assert config.group_loc_threshold == 300

    def test_from_dict_empty(self):
        config = ImplementConfig.from_dict({})
        assert config.group_loc_threshold == 300

    def test_from_dict_none(self):
        config = ImplementConfig.from_dict(None)
        assert config.group_loc_threshold == 300


class TestImplementConfigLoad:
    """Tests for ImplementConfig.load() from se3.yaml."""

    def test_defaults_when_no_file(self, tmp_path):
        config = ImplementConfig.load(tmp_path)
        assert config.group_loc_threshold == 300

    def test_defaults_when_no_implement_section(self, tmp_path):
        (tmp_path / "se3.yaml").write_text("version:\n  enabled: true\n")
        config = ImplementConfig.load(tmp_path)
        assert config.group_loc_threshold == 300

    def test_custom_threshold(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            "implement:\n  group_loc_threshold: 500\n"
        )
        config = ImplementConfig.load(tmp_path)
        assert config.group_loc_threshold == 500

    def test_handles_invalid_yaml(self, tmp_path):
        (tmp_path / "se3.yaml").write_text("{{invalid yaml")
        config = ImplementConfig.load(tmp_path)
        assert config.group_loc_threshold == 300

    def test_handles_non_dict_implement_section(self, tmp_path):
        (tmp_path / "se3.yaml").write_text("implement: true\n")
        config = ImplementConfig.load(tmp_path)
        assert config.group_loc_threshold == 300

    def test_from_dict_custom(self):
        config = ImplementConfig.from_dict({"group_loc_threshold": 150})
        assert config.group_loc_threshold == 150
