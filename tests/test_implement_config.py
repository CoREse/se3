"""Tests for ImplementConfig configuration."""

from __future__ import annotations

import pytest

from tianluo.config import ImplementConfig, _coerce_bool


class TestImplementConfigDefaults:
    """Tests for ImplementConfig default values."""

    def test_default_threshold(self):
        config = ImplementConfig()
        assert config.group_loc_threshold == 300

    def test_default_use_worktree(self):
        config = ImplementConfig()
        assert config.use_worktree is True

    def test_from_dict_empty(self):
        config = ImplementConfig.from_dict({})
        assert config.group_loc_threshold == 300
        assert config.use_worktree is True

    def test_from_dict_none(self):
        config = ImplementConfig.from_dict(None)
        assert config.group_loc_threshold == 300
        assert config.use_worktree is True


class TestImplementConfigLoad:
    """Tests for ImplementConfig.load() from se3.yaml."""

    def test_defaults_when_no_file(self, tmp_path):
        config = ImplementConfig.load(tmp_path)
        assert config.group_loc_threshold == 300
        assert config.use_worktree is True

    def test_defaults_when_no_implement_section(self, tmp_path):
        (tmp_path / "se3.yaml").write_text("version:\n  enabled: true\n")
        config = ImplementConfig.load(tmp_path)
        assert config.group_loc_threshold == 300
        assert config.use_worktree is True

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
        assert config.use_worktree is True

    def test_handles_non_dict_implement_section(self, tmp_path):
        (tmp_path / "se3.yaml").write_text("implement: true\n")
        config = ImplementConfig.load(tmp_path)
        assert config.group_loc_threshold == 300
        assert config.use_worktree is True

    def test_from_dict_custom(self):
        config = ImplementConfig.from_dict({"group_loc_threshold": 150})
        assert config.group_loc_threshold == 150


class TestImplementConfigUseWorktree:
    """Tests for ImplementConfig.use_worktree field."""

    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch):
        monkeypatch.delenv("SE3_IMPLEMENT_USE_WORKTREE", raising=False)

    def test_from_dict_explicit_true(self):
        config = ImplementConfig.from_dict({"use_worktree": True})
        assert config.use_worktree is True

    def test_from_dict_explicit_false(self):
        config = ImplementConfig.from_dict({"use_worktree": False})
        assert config.use_worktree is False

    def test_from_dict_string_false(self):
        config = ImplementConfig.from_dict({"use_worktree": "false"})
        assert config.use_worktree is False

    def test_from_dict_string_true(self):
        config = ImplementConfig.from_dict({"use_worktree": "true"})
        assert config.use_worktree is True

    def test_from_dict_string_zero(self):
        config = ImplementConfig.from_dict({"use_worktree": "0"})
        assert config.use_worktree is False

    def test_from_dict_string_one(self):
        config = ImplementConfig.from_dict({"use_worktree": "1"})
        assert config.use_worktree is True

    def test_from_dict_string_case_insensitive(self):
        assert ImplementConfig.from_dict({"use_worktree": "FALSE"}).use_worktree is False
        assert ImplementConfig.from_dict({"use_worktree": "True"}).use_worktree is True

    def test_from_dict_invalid_string_falls_back_to_default(self):
        # Unknown strings don't silently flip; default (True) is retained.
        config = ImplementConfig.from_dict({"use_worktree": "maybe"})
        assert config.use_worktree is True

    def test_load_yaml_false(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            "implement:\n  use_worktree: false\n"
        )
        config = ImplementConfig.load(tmp_path)
        assert config.use_worktree is False

    def test_load_yaml_true(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            "implement:\n  use_worktree: true\n"
        )
        config = ImplementConfig.load(tmp_path)
        assert config.use_worktree is True

    def test_load_yaml_string_false(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            'implement:\n  use_worktree: "false"\n'
        )
        config = ImplementConfig.load(tmp_path)
        assert config.use_worktree is False

    def test_load_unset_preserves_default(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            "implement:\n  group_loc_threshold: 500\n"
        )
        config = ImplementConfig.load(tmp_path)
        assert config.use_worktree is True
        assert config.group_loc_threshold == 500

    def test_env_var_overrides_to_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SE3_IMPLEMENT_USE_WORKTREE", "0")
        config = ImplementConfig.load(tmp_path)
        assert config.use_worktree is False

    def test_env_var_overrides_yaml_true(self, tmp_path, monkeypatch):
        (tmp_path / "se3.yaml").write_text(
            "implement:\n  use_worktree: true\n"
        )
        monkeypatch.setenv("SE3_IMPLEMENT_USE_WORKTREE", "0")
        config = ImplementConfig.load(tmp_path)
        assert config.use_worktree is False

    def test_env_var_overrides_yaml_false(self, tmp_path, monkeypatch):
        (tmp_path / "se3.yaml").write_text(
            "implement:\n  use_worktree: false\n"
        )
        monkeypatch.setenv("SE3_IMPLEMENT_USE_WORKTREE", "1")
        config = ImplementConfig.load(tmp_path)
        assert config.use_worktree is True

    def test_env_var_false_string(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SE3_IMPLEMENT_USE_WORKTREE", "false")
        config = ImplementConfig.load(tmp_path)
        assert config.use_worktree is False

    def test_env_var_invalid_falls_back_to_yaml(self, tmp_path, monkeypatch):
        (tmp_path / "se3.yaml").write_text(
            "implement:\n  use_worktree: false\n"
        )
        monkeypatch.setenv("SE3_IMPLEMENT_USE_WORKTREE", "garbage")
        config = ImplementConfig.load(tmp_path)
        # Invalid env value falls back to whatever the YAML produced.
        assert config.use_worktree is False


class TestCoerceBool:
    """Tests for the _coerce_bool helper."""

    def test_float_zero(self):
        # 0.0 coerces to False via standard Python bool() semantics.
        assert _coerce_bool(0.0, default=True) is False

    def test_float_non_zero(self):
        # Non-zero floats coerce to True.
        assert _coerce_bool(1.5, default=False) is True
        assert _coerce_bool(-0.1, default=False) is True

    def test_none_falls_back_to_default(self):
        # None falls through every branch and returns default.
        assert _coerce_bool(None, default=True) is True
        assert _coerce_bool(None, default=False) is False
