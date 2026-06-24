"""Tests for the G8 config-knob basis of the code-index + charter system.

This module currently covers ``CodeIndexConfig`` — the ``se3 config`` knobs the
code-index subsystem consumes (degrade thresholds, chunk granularity, and the
explicit-exclude list). It is named ``test_charter`` because it is the shared
home for the new code-index/charter config surface; charter-specific tests join
it once the charter module lands in a later group.
"""

from __future__ import annotations

import sys

import pytest

from se3.config import (
    DEFAULT_CODE_INDEX_CHUNK_BYTES,
    DEFAULT_CODE_INDEX_CHUNK_LINES,
    DEFAULT_CODE_INDEX_DEGRADE_TRIGGER_BYTES,
    DEFAULT_CODE_INDEX_DEGRADE_TRIGGER_LINES,
    CodeIndexConfig,
    load_code_index_config,
)


class TestCodeIndexConfigDefaults:
    """Default values when nothing is configured (built-in defaults)."""

    def test_dataclass_defaults(self):
        cfg = CodeIndexConfig()
        assert cfg.degrade_trigger_lines == 2000
        assert cfg.degrade_trigger_bytes == 256 * 1024
        assert cfg.chunk_lines == 200
        assert cfg.chunk_bytes == 16 * 1024
        assert cfg.exclude == []

    def test_module_constants_match_dataclass_defaults(self):
        cfg = CodeIndexConfig()
        assert cfg.degrade_trigger_lines == DEFAULT_CODE_INDEX_DEGRADE_TRIGGER_LINES
        assert cfg.degrade_trigger_bytes == DEFAULT_CODE_INDEX_DEGRADE_TRIGGER_BYTES
        assert cfg.chunk_lines == DEFAULT_CODE_INDEX_CHUNK_LINES
        assert cfg.chunk_bytes == DEFAULT_CODE_INDEX_CHUNK_BYTES

    def test_exclude_default_is_independent_list(self):
        # field(default_factory=list) — each instance gets its own list.
        a = CodeIndexConfig()
        b = CodeIndexConfig()
        a.exclude.append("foo")
        assert b.exclude == []

    def test_from_dict_empty(self):
        assert CodeIndexConfig.from_dict({}) == CodeIndexConfig()

    def test_from_dict_none(self):
        assert CodeIndexConfig.from_dict(None) == CodeIndexConfig()

    def test_from_dict_non_dict(self):
        assert CodeIndexConfig.from_dict("nonsense") == CodeIndexConfig()


class TestCodeIndexConfigOverride:
    """``se3.yaml`` overrides and ``load()`` — code_index.* takes effect."""

    def test_load_defaults_when_no_file(self, tmp_path):
        # Acceptance: absent config returns built-in defaults.
        cfg = CodeIndexConfig.load(tmp_path)
        assert cfg == CodeIndexConfig()

    def test_load_defaults_when_no_section(self, tmp_path):
        (tmp_path / "se3.yaml").write_text("version:\n  enabled: true\n")
        cfg = CodeIndexConfig.load(tmp_path)
        assert cfg == CodeIndexConfig()

    def test_yaml_override_takes_effect(self, tmp_path):
        # Acceptance: se3.yaml code_index.* override is honored.
        (tmp_path / "se3.yaml").write_text(
            "code_index:\n"
            "  degrade_trigger_lines: 5000\n"
            "  degrade_trigger_bytes: 524288\n"
            "  chunk_lines: 100\n"
            "  chunk_bytes: 8192\n"
            "  exclude:\n"
            "    - vendor/\n"
            "    - generated/big.json\n"
        )
        cfg = CodeIndexConfig.load(tmp_path)
        assert cfg.degrade_trigger_lines == 5000
        assert cfg.degrade_trigger_bytes == 524288
        assert cfg.chunk_lines == 100
        assert cfg.chunk_bytes == 8192
        assert cfg.exclude == ["vendor/", "generated/big.json"]

    def test_partial_override_keeps_other_defaults(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            "code_index:\n  chunk_lines: 50\n"
        )
        cfg = CodeIndexConfig.load(tmp_path)
        assert cfg.chunk_lines == 50
        assert cfg.degrade_trigger_lines == DEFAULT_CODE_INDEX_DEGRADE_TRIGGER_LINES
        assert cfg.chunk_bytes == DEFAULT_CODE_INDEX_CHUNK_BYTES
        assert cfg.exclude == []

    def test_load_code_index_config_helper(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            "code_index:\n  degrade_trigger_bytes: 1234\n"
        )
        cfg = load_code_index_config(tmp_path)
        assert cfg.degrade_trigger_bytes == 1234


class TestCodeIndexConfigExclude:
    """``exclude`` is the explicit-exclude (project-specific) list."""

    def test_exclude_is_explicit_list(self):
        cfg = CodeIndexConfig.from_dict({"exclude": ["a.py", "dir/"]})
        assert cfg.exclude == ["a.py", "dir/"]

    def test_exclude_entries_are_stripped(self):
        cfg = CodeIndexConfig.from_dict({"exclude": ["  spaced/  "]})
        assert cfg.exclude == ["spaced/"]

    def test_exclude_non_list_falls_back_to_empty(self):
        cfg = CodeIndexConfig.from_dict({"exclude": "not-a-list"})
        assert cfg.exclude == []

    def test_exclude_drops_non_string_and_blank_entries(self):
        cfg = CodeIndexConfig.from_dict(
            {"exclude": ["keep.py", "", "   ", 42, None, "also-keep/"]}
        )
        assert cfg.exclude == ["keep.py", "also-keep/"]

    def test_exclude_absent_defaults_empty(self):
        cfg = CodeIndexConfig.from_dict({"chunk_lines": 10})
        assert cfg.exclude == []


class TestCodeIndexConfigFaultTolerance:
    """Illegal values fall back to defaults and never raise."""

    def test_negative_falls_back(self):
        cfg = CodeIndexConfig.from_dict({"degrade_trigger_lines": -5})
        assert cfg.degrade_trigger_lines == DEFAULT_CODE_INDEX_DEGRADE_TRIGGER_LINES

    def test_zero_falls_back(self):
        cfg = CodeIndexConfig.from_dict({"chunk_lines": 0})
        assert cfg.chunk_lines == DEFAULT_CODE_INDEX_CHUNK_LINES

    def test_non_integer_falls_back(self):
        cfg = CodeIndexConfig.from_dict({"chunk_bytes": "big"})
        assert cfg.chunk_bytes == DEFAULT_CODE_INDEX_CHUNK_BYTES

    def test_bool_falls_back(self):
        # bool is an int subclass — must be rejected explicitly.
        cfg = CodeIndexConfig.from_dict({"degrade_trigger_bytes": True})
        assert cfg.degrade_trigger_bytes == DEFAULT_CODE_INDEX_DEGRADE_TRIGGER_BYTES

    def test_float_falls_back(self):
        cfg = CodeIndexConfig.from_dict({"chunk_lines": 200.0})
        assert cfg.chunk_lines == DEFAULT_CODE_INDEX_CHUNK_LINES

    def test_invalid_yaml_falls_back_to_defaults(self, tmp_path):
        (tmp_path / "se3.yaml").write_text("{{invalid yaml")
        cfg = CodeIndexConfig.load(tmp_path)
        assert cfg == CodeIndexConfig()

    def test_non_dict_section_falls_back(self, tmp_path):
        (tmp_path / "se3.yaml").write_text("code_index: true\n")
        cfg = CodeIndexConfig.load(tmp_path)
        assert cfg == CodeIndexConfig()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
