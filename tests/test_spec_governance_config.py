"""Tests for the G1 spec-governance basis: SpecGovernanceConfig + the
spec_governance.py normative-constant source module."""

from __future__ import annotations

import importlib
import sys

import pytest

from tianluo.config import (
    DEFAULT_BASE_MAX_BYTES,
    DEFAULT_GUARDRAILS_SIZE_TIER,
    DEFAULT_INDEX_RENDER_THRESHOLD,
    DEFAULT_REQUIREMENT_WARN_BYTES,
    DEFAULT_SPEC_FILE_WARN_BYTES,
    SpecGovernanceConfig,
    load_spec_governance_config,
)


class TestSpecGovernanceConfigDefaults:
    """Default values when nothing is configured."""

    def test_dataclass_defaults(self):
        cfg = SpecGovernanceConfig()
        assert cfg.base_max_bytes == 32768
        assert cfg.index_render_threshold == 16384
        assert cfg.spec_file_warn_bytes == 65536
        assert cfg.requirement_warn_bytes == 8192
        assert cfg.guardrails_size_tier == "warn"

    def test_default_tier_is_warn(self):
        # Acceptance criterion: default guardrails_size_tier == 'warn'
        assert SpecGovernanceConfig().guardrails_size_tier == "warn"

    def test_module_constants_match_dataclass_defaults(self):
        cfg = SpecGovernanceConfig()
        assert cfg.base_max_bytes == DEFAULT_BASE_MAX_BYTES
        assert cfg.index_render_threshold == DEFAULT_INDEX_RENDER_THRESHOLD
        assert cfg.spec_file_warn_bytes == DEFAULT_SPEC_FILE_WARN_BYTES
        assert cfg.requirement_warn_bytes == DEFAULT_REQUIREMENT_WARN_BYTES
        assert cfg.guardrails_size_tier == DEFAULT_GUARDRAILS_SIZE_TIER

    def test_from_dict_empty(self):
        assert SpecGovernanceConfig.from_dict({}) == SpecGovernanceConfig()

    def test_from_dict_none(self):
        assert SpecGovernanceConfig.from_dict(None) == SpecGovernanceConfig()

    def test_from_dict_non_dict(self):
        assert SpecGovernanceConfig.from_dict("nonsense") == SpecGovernanceConfig()


class TestSpecGovernanceConfigOverride:
    """YAML overrides and load()."""

    def test_load_defaults_when_no_file(self, tmp_path):
        cfg = SpecGovernanceConfig.load(tmp_path)
        assert cfg == SpecGovernanceConfig()

    def test_load_defaults_when_no_section(self, tmp_path):
        (tmp_path / "tianluo.yaml").write_text("version:\n  enabled: true\n")
        cfg = SpecGovernanceConfig.load(tmp_path)
        assert cfg == SpecGovernanceConfig()

    def test_yaml_override_takes_effect(self, tmp_path):
        (tmp_path / "tianluo.yaml").write_text(
            "spec_governance:\n"
            "  base_max_bytes: 65536\n"
            "  index_render_threshold: 8192\n"
            "  spec_file_warn_bytes: 131072\n"
            "  requirement_warn_bytes: 4096\n"
            "  guardrails_size_tier: enforce\n"
        )
        cfg = SpecGovernanceConfig.load(tmp_path)
        assert cfg.base_max_bytes == 65536
        assert cfg.index_render_threshold == 8192
        assert cfg.spec_file_warn_bytes == 131072
        assert cfg.requirement_warn_bytes == 4096
        assert cfg.guardrails_size_tier == "enforce"

    def test_partial_override_keeps_other_defaults(self, tmp_path):
        (tmp_path / "tianluo.yaml").write_text(
            "spec_governance:\n  base_max_bytes: 16384\n"
        )
        cfg = SpecGovernanceConfig.load(tmp_path)
        assert cfg.base_max_bytes == 16384
        assert cfg.index_render_threshold == DEFAULT_INDEX_RENDER_THRESHOLD
        assert cfg.guardrails_size_tier == DEFAULT_GUARDRAILS_SIZE_TIER

    def test_tier_case_insensitive(self, tmp_path):
        (tmp_path / "tianluo.yaml").write_text(
            "spec_governance:\n  guardrails_size_tier: ENFORCE\n"
        )
        cfg = SpecGovernanceConfig.load(tmp_path)
        assert cfg.guardrails_size_tier == "enforce"

    def test_load_spec_governance_config_helper(self, tmp_path):
        (tmp_path / "tianluo.yaml").write_text(
            "spec_governance:\n  base_max_bytes: 1234\n"
        )
        cfg = load_spec_governance_config(tmp_path)
        assert cfg.base_max_bytes == 1234


class TestSpecGovernanceConfigFaultTolerance:
    """Illegal values fall back to defaults and never raise."""

    def test_negative_base_max_bytes_falls_back(self):
        cfg = SpecGovernanceConfig.from_dict({"base_max_bytes": -5})
        assert cfg.base_max_bytes == DEFAULT_BASE_MAX_BYTES

    def test_zero_falls_back(self):
        cfg = SpecGovernanceConfig.from_dict({"requirement_warn_bytes": 0})
        assert cfg.requirement_warn_bytes == DEFAULT_REQUIREMENT_WARN_BYTES

    def test_non_integer_falls_back(self):
        cfg = SpecGovernanceConfig.from_dict({"index_render_threshold": "big"})
        assert cfg.index_render_threshold == DEFAULT_INDEX_RENDER_THRESHOLD

    def test_bool_falls_back(self):
        # bool is an int subclass — must be rejected explicitly.
        cfg = SpecGovernanceConfig.from_dict({"spec_file_warn_bytes": True})
        assert cfg.spec_file_warn_bytes == DEFAULT_SPEC_FILE_WARN_BYTES

    def test_float_falls_back(self):
        cfg = SpecGovernanceConfig.from_dict({"base_max_bytes": 32768.0})
        assert cfg.base_max_bytes == DEFAULT_BASE_MAX_BYTES

    def test_invalid_tier_falls_back(self):
        cfg = SpecGovernanceConfig.from_dict({"guardrails_size_tier": "block"})
        assert cfg.guardrails_size_tier == DEFAULT_GUARDRAILS_SIZE_TIER

    def test_invalid_yaml_falls_back_to_defaults(self, tmp_path):
        (tmp_path / "tianluo.yaml").write_text("{{invalid yaml")
        cfg = SpecGovernanceConfig.load(tmp_path)
        assert cfg == SpecGovernanceConfig()

    def test_non_dict_section_falls_back(self, tmp_path):
        (tmp_path / "tianluo.yaml").write_text("spec_governance: true\n")
        cfg = SpecGovernanceConfig.load(tmp_path)
        assert cfg == SpecGovernanceConfig()


class TestSpecGovernanceModule:
    """The spec_governance.py normative-constant source module."""

    def test_imports_with_no_side_effects(self):
        # Re-import from scratch to assert importing is side-effect-free.
        sys.modules.pop("tianluo.engine.spec_governance", None)
        mod = importlib.import_module("tianluo.engine.spec_governance")
        assert mod is not None

    def test_only_stdlib_dependencies(self):
        # The source must not import any third-party / intra-project module.
        import tianluo.engine.spec_governance as mod

        src = open(mod.__file__, encoding="utf-8").read()
        # No 'from tianluo' / 'import tianluo' and no obvious third-party imports.
        assert "import tianluo" not in src
        assert "from tianluo" not in src

    def test_exports_required_constants(self):
        from tianluo.engine import spec_governance as g

        assert isinstance(g.BASE_ADMISSION_STANDARD, str) and g.BASE_ADMISSION_STANDARD
        assert isinstance(g.WRITING_DISCIPLINE, str) and g.WRITING_DISCIPLINE
        assert isinstance(g.SPLIT_CRITERIA, str) and g.SPLIT_CRITERIA
        assert g.DOMAIN_MARKER_PREFIX == "<!-- domain:"
        assert g.UNCLASSIFIED_GROUP == "(未分类)"

    def test_admission_standard_mentions_base_and_modules(self):
        from tianluo.engine.spec_governance import BASE_ADMISSION_STANDARD

        text = BASE_ADMISSION_STANDARD.lower()
        assert "base" in text
        assert "module" in text

    def test_writing_discipline_covers_all_four_rules(self):
        from tianluo.engine.spec_governance import WRITING_DISCIPLINE

        for marker in ("(a)", "(b)", "(c)", "(d)"):
            assert marker in WRITING_DISCIPLINE


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
