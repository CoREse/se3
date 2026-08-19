"""Regression: the spec loading/validation dead code is gone for good.

``tianluo/specs/`` is retired, so the modules that parsed, validated and
governed those files — and the config blocks that tuned them — have no subject
left. These tests pin the *absence* of that surface, and pin the one compatible
behaviour that must survive it: a ``tianluo.yaml`` still carrying the retired
blocks loads without error, because config loading reads named sections rather
than validating a whole-file schema.
"""

from __future__ import annotations

import importlib

import pytest

RETIRED_MODULES = [
    "tianluo.engine.spec_validator",
    "tianluo.engine.spec_governance",
    "tianluo.engine.spec_format",
]

LEGACY_CONFIG_YAML = """\
spec_loading:
  steps:
    implement: full_spec
spec_governance:
  base_max_bytes: 16384
  guardrails_size_tier: enforce
spec_write_protection:
  enabled: true
merge:
  guardrail_repair:
    max_iterations: 3
workflow:
  max_fix_iterations: 7
"""


class TestRetiredModulesGone:
    """The spec parse/validate/govern modules no longer exist in the package."""

    @pytest.mark.parametrize("module_name", RETIRED_MODULES)
    def test_module_not_importable(self, module_name):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module_name)

    def test_package_dir_has_no_spec_modules(self):
        import tianluo.engine as engine_pkg
        from pathlib import Path

        engine_dir = Path(engine_pkg.__file__).parent
        for name in ("spec_validator.py", "spec_governance.py", "spec_format.py"):
            assert not (engine_dir / name).exists(), name


class TestRetiredConfigLoaders:
    """The spec-era config loaders are gone; their YAML blocks are inert."""

    @pytest.mark.parametrize(
        "symbol",
        [
            "SpecLoadingConfig",
            "load_spec_loading_config",
            "SpecGovernanceConfig",
            "load_spec_governance_config",
        ],
    )
    def test_loader_symbol_removed(self, symbol):
        import tianluo.config as config

        assert not hasattr(config, symbol)

    def test_legacy_blocks_load_without_error(self, tmp_path):
        """A config still carrying the retired blocks must load silently.

        Users upgrade in place; a leftover block may never be edited out, so it
        has to be ignored rather than rejected.
        """
        (tmp_path / "tianluo.yaml").write_text(LEGACY_CONFIG_YAML)

        from tianluo.config import load_project_yaml, load_workflow_config

        data, _src = load_project_yaml(tmp_path)
        assert "spec_governance" in data  # the block parses; nothing consumes it

        # A live loader sitting in the same file is unaffected by the residue.
        assert load_workflow_config(tmp_path).max_fix_iterations == 7

    def test_legacy_blocks_do_not_break_merge_config(self, tmp_path):
        """``merge.guardrail_repair`` residue must not disturb merge loading."""
        (tmp_path / "tianluo.yaml").write_text(LEGACY_CONFIG_YAML)

        from tianluo.config import load_merge_config

        # Loading must succeed; the retired sub-block is simply not read.
        assert load_merge_config(tmp_path) is not None


class TestSpecLoadingSurfaceRemoved:
    """No module still resolves a specs dir or reads spec files."""

    def test_context_builder_class_removed(self):
        from tianluo.engine import context_builder

        assert not hasattr(context_builder, "ContextBuilder")
        assert not hasattr(context_builder, "get_spec_names_injection")

    def test_utils_spec_helpers_removed(self):
        from tianluo import utils

        for symbol in (
            "_resolve_specs_dir",
            "discover_specs",
            "parse_spec",
            "discover_changes",
            "discover_specs_in_change",
        ):
            assert not hasattr(utils, symbol), symbol

    def test_project_context_collects_no_specs(self, tmp_path):
        """The collector drops the (always-empty) specs key but still works."""
        from tianluo.engine.project_context import ProjectContextCollector

        assert not hasattr(ProjectContextCollector, "_collect_specs")

        collected = ProjectContextCollector(tmp_path).collect()
        assert "specs" not in collected
        assert set(collected) == {"git", "flow_engine", "backlog"}


class TestAnalyzeProjectSummaryStillBuilds:
    """analyze's project-context summary survives the specs collection removal."""

    def test_summary_reports_branch(self, tmp_path):
        from tianluo.engine.steps.analyze import _collect_project_summary

        summary = _collect_project_summary(tmp_path)

        assert isinstance(summary, str)
        assert summary
        assert "Available specs" not in summary
