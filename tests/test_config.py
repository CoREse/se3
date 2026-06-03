"""Tests for WorkflowConfig and related config utilities.

Covers:
- WorkflowConfig.from_dict defaults, validation, and coercion
- WorkflowConfig.load from se3.yaml
- load_workflow_config convenience function
- get_max_fix_iterations refactored to use WorkflowConfig
"""

from __future__ import annotations

from pathlib import Path

import pytest

from se3.config import (
    ConfigError,
    DEFAULT_MAX_FIX_ITERATIONS,
    DEFAULT_SELF_CHECK_CONVERGENCE_ENABLED,
    DEFAULT_SELF_CHECK_PASSES_REQUIRED,
    TestConfig,
    WorkflowConfig,
    get_max_fix_iterations,
    load_workflow_config,
)


# ---------------------------------------------------------------------------
# WorkflowConfig.from_dict
# ---------------------------------------------------------------------------

class TestWorkflowConfigFromDict:
    def test_empty_dict_returns_defaults(self):
        cfg = WorkflowConfig.from_dict({})
        assert cfg.max_fix_iterations == DEFAULT_MAX_FIX_ITERATIONS
        assert cfg.self_check_passes_required == DEFAULT_SELF_CHECK_PASSES_REQUIRED
        assert cfg.self_check_convergence_enabled == DEFAULT_SELF_CHECK_CONVERGENCE_ENABLED

    def test_none_returns_defaults(self):
        cfg = WorkflowConfig.from_dict({})
        assert cfg.max_fix_iterations == DEFAULT_MAX_FIX_ITERATIONS
        assert cfg.self_check_passes_required == DEFAULT_SELF_CHECK_PASSES_REQUIRED
        assert cfg.self_check_convergence_enabled == DEFAULT_SELF_CHECK_CONVERGENCE_ENABLED

    def test_default_values_when_workflow_section_missing(self):
        cfg = WorkflowConfig.from_dict({"version": {"enabled": False}})
        assert cfg.max_fix_iterations == DEFAULT_MAX_FIX_ITERATIONS
        assert cfg.self_check_passes_required == DEFAULT_SELF_CHECK_PASSES_REQUIRED
        assert cfg.self_check_convergence_enabled == DEFAULT_SELF_CHECK_CONVERGENCE_ENABLED

    def test_custom_max_fix_iterations(self):
        cfg = WorkflowConfig.from_dict({"workflow": {"max_fix_iterations": 10}})
        assert cfg.max_fix_iterations == 10
        assert cfg.self_check_passes_required == DEFAULT_SELF_CHECK_PASSES_REQUIRED
        assert cfg.self_check_convergence_enabled == DEFAULT_SELF_CHECK_CONVERGENCE_ENABLED

    def test_custom_self_check_passes_required(self):
        cfg = WorkflowConfig.from_dict({"workflow": {"self_check_passes_required": 3}})
        assert cfg.max_fix_iterations == DEFAULT_MAX_FIX_ITERATIONS
        assert cfg.self_check_passes_required == 3
        assert cfg.self_check_convergence_enabled == DEFAULT_SELF_CHECK_CONVERGENCE_ENABLED

    def test_custom_convergence_enabled_true(self):
        cfg = WorkflowConfig.from_dict(
            {"workflow": {"self_check_convergence_enabled": True}}
        )
        assert cfg.self_check_convergence_enabled is True

    def test_convergence_enabled_string_true(self):
        """String 'true' should be coerced to bool True."""
        cfg = WorkflowConfig.from_dict(
            {"workflow": {"self_check_convergence_enabled": "true"}}
        )
        assert cfg.self_check_convergence_enabled is True

    def test_convergence_enabled_string_false(self):
        """String 'false' should be coerced to bool False."""
        cfg = WorkflowConfig.from_dict(
            {"workflow": {"self_check_convergence_enabled": "false"}}
        )
        assert cfg.self_check_convergence_enabled is False

    def test_passes_required_string_coercion(self):
        """String-shaped integer should be tolerated."""
        cfg = WorkflowConfig.from_dict(
            {"workflow": {"self_check_passes_required": "5"}}
        )
        assert cfg.self_check_passes_required == 5

    def test_passes_required_zero_raises(self):
        with pytest.raises(ConfigError, match="must be >= 1"):
            WorkflowConfig.from_dict(
                {"workflow": {"self_check_passes_required": 0}}
            )

    def test_passes_required_negative_raises(self):
        with pytest.raises(ConfigError, match="must be >= 1"):
            WorkflowConfig.from_dict(
                {"workflow": {"self_check_passes_required": -1}}
            )

    def test_passes_required_zero_string_raises(self):
        with pytest.raises(ConfigError, match="must be >= 1"):
            WorkflowConfig.from_dict(
                {"workflow": {"self_check_passes_required": "0"}}
            )

    def test_passes_required_float_falls_back_to_default(self, caplog):
        """Floats for self_check_passes_required warn and fall back to the
        default. Out-of-scope of the unlimited-sentinel work, so we keep
        the historical tolerant behavior rather than tightening to fail-fast.
        """
        import logging as _logging

        with caplog.at_level(_logging.WARNING):
            cfg = WorkflowConfig.from_dict(
                {"workflow": {"self_check_passes_required": 0.5}}
            )
        assert cfg.self_check_passes_required == DEFAULT_SELF_CHECK_PASSES_REQUIRED

        with caplog.at_level(_logging.WARNING):
            cfg = WorkflowConfig.from_dict(
                {"workflow": {"self_check_passes_required": 2.0}}
            )
        assert cfg.self_check_passes_required == DEFAULT_SELF_CHECK_PASSES_REQUIRED

    def test_passes_required_bool_falls_back_to_default(self, caplog):
        """Booleans for self_check_passes_required warn and fall back to the
        default rather than coercing to int (True -> 1 / False -> 0)."""
        import logging as _logging

        with caplog.at_level(_logging.WARNING):
            cfg = WorkflowConfig.from_dict(
                {"workflow": {"self_check_passes_required": True}}
            )
        assert cfg.self_check_passes_required == DEFAULT_SELF_CHECK_PASSES_REQUIRED

        with caplog.at_level(_logging.WARNING):
            cfg = WorkflowConfig.from_dict(
                {"workflow": {"self_check_passes_required": False}}
            )
        assert cfg.self_check_passes_required == DEFAULT_SELF_CHECK_PASSES_REQUIRED

    def test_all_fields_custom(self):
        cfg = WorkflowConfig.from_dict(
            {
                "workflow": {
                    "max_fix_iterations": 15,
                    "self_check_passes_required": 2,
                    "self_check_convergence_enabled": True,
                }
            }
        )
        assert cfg.max_fix_iterations == 15
        assert cfg.self_check_passes_required == 2
        assert cfg.self_check_convergence_enabled is True

    def test_invalid_max_fix_iterations_falls_back_to_default(self):
        """Non-integer max_fix_iterations falls back to default."""
        cfg = WorkflowConfig.from_dict(
            {"workflow": {"max_fix_iterations": "not_a_number"}}
        )
        assert cfg.max_fix_iterations == DEFAULT_MAX_FIX_ITERATIONS

    def test_default_value_is_100(self):
        """The default value for max_fix_iterations is 100."""
        assert DEFAULT_MAX_FIX_ITERATIONS == 100
        assert WorkflowConfig().max_fix_iterations == 100

    def test_max_fix_iterations_zero_sentinel(self):
        """0 is a valid sentinel meaning 'unlimited' — preserved as-is."""
        cfg = WorkflowConfig.from_dict({"workflow": {"max_fix_iterations": 0}})
        assert cfg.max_fix_iterations == 0

    def test_max_fix_iterations_null_sentinel(self):
        """null/None is normalized to the sentinel 0."""
        cfg = WorkflowConfig.from_dict({"workflow": {"max_fix_iterations": None}})
        assert cfg.max_fix_iterations == 0

    def test_max_fix_iterations_string_zero_preserved(self):
        """String '0' is coerced to int 0 (sentinel)."""
        cfg = WorkflowConfig.from_dict({"workflow": {"max_fix_iterations": "0"}})
        assert cfg.max_fix_iterations == 0

    def test_max_fix_iterations_negative_raises(self):
        """Negative max_fix_iterations is rejected fail-fast (mirrors
        self_check_passes_required); negatives must NOT be silently treated
        as the unlimited sentinel.
        """
        with pytest.raises(ConfigError, match="must be >= 0"):
            WorkflowConfig.from_dict({"workflow": {"max_fix_iterations": -1}})

    def test_max_fix_iterations_negative_string_raises(self):
        """String-shaped negatives are also rejected."""
        with pytest.raises(ConfigError, match="must be >= 0"):
            WorkflowConfig.from_dict({"workflow": {"max_fix_iterations": "-5"}})

    def test_max_fix_iterations_bool_warns_and_falls_back(self):
        """bool max_fix_iterations warns and falls back to default —
        symmetric with self_check_passes_required."""
        cfg = WorkflowConfig.from_dict({"workflow": {"max_fix_iterations": True}})
        assert cfg.max_fix_iterations == DEFAULT_MAX_FIX_ITERATIONS

        cfg = WorkflowConfig.from_dict({"workflow": {"max_fix_iterations": False}})
        assert cfg.max_fix_iterations == DEFAULT_MAX_FIX_ITERATIONS

    def test_max_fix_iterations_float_warns_and_falls_back(self):
        """Float max_fix_iterations warns and falls back to default —
        symmetric with self_check_passes_required."""
        cfg = WorkflowConfig.from_dict({"workflow": {"max_fix_iterations": 0.5}})
        assert cfg.max_fix_iterations == DEFAULT_MAX_FIX_ITERATIONS

        cfg = WorkflowConfig.from_dict({"workflow": {"max_fix_iterations": -0.5}})
        assert cfg.max_fix_iterations == DEFAULT_MAX_FIX_ITERATIONS

        cfg = WorkflowConfig.from_dict({"workflow": {"max_fix_iterations": 5.0}})
        assert cfg.max_fix_iterations == DEFAULT_MAX_FIX_ITERATIONS

    def test_workflow_section_is_non_dict(self):
        """When workflow is not a dict, defaults are used."""
        cfg = WorkflowConfig.from_dict({"workflow": "invalid"})
        assert cfg.max_fix_iterations == DEFAULT_MAX_FIX_ITERATIONS
        assert cfg.self_check_passes_required == DEFAULT_SELF_CHECK_PASSES_REQUIRED
        assert cfg.self_check_convergence_enabled == DEFAULT_SELF_CHECK_CONVERGENCE_ENABLED


# ---------------------------------------------------------------------------
# WorkflowConfig.load
# ---------------------------------------------------------------------------

class TestWorkflowConfigLoad:
    def test_load_from_yaml_file(self, tmp_path: Path):
        yaml_content = """
workflow:
  max_fix_iterations: 10
  self_check_passes_required: 3
  self_check_convergence_enabled: true
"""
        se3_yaml = tmp_path / "se3.yaml"
        se3_yaml.write_text(yaml_content, encoding="utf-8")

        cfg = WorkflowConfig.load(tmp_path)
        assert cfg.max_fix_iterations == 10
        assert cfg.self_check_passes_required == 3
        assert cfg.self_check_convergence_enabled is True

    def test_load_no_config_file(self, tmp_path: Path):
        """When no se3.yaml exists, defaults are returned."""
        cfg = WorkflowConfig.load(tmp_path)
        assert cfg.max_fix_iterations == DEFAULT_MAX_FIX_ITERATIONS
        assert cfg.self_check_passes_required == DEFAULT_SELF_CHECK_PASSES_REQUIRED
        assert cfg.self_check_convergence_enabled == DEFAULT_SELF_CHECK_CONVERGENCE_ENABLED

    def test_load_fail_fast(self, tmp_path: Path):
        """Invalid self_check_passes_required raises ConfigError from load()."""
        yaml_content = """
workflow:
  self_check_passes_required: 0
"""
        se3_yaml = tmp_path / "se3.yaml"
        se3_yaml.write_text(yaml_content, encoding="utf-8")

        with pytest.raises(ConfigError, match="must be >= 1"):
            WorkflowConfig.load(tmp_path)

    def test_load_logs_effective_source_se3_yaml(self, tmp_path: Path, caplog):
        """load() logs which file the resolved max_fix_iterations came from."""
        import logging as _logging

        (tmp_path / "se3.yaml").write_text(
            "workflow:\n  max_fix_iterations: 42\n", encoding="utf-8"
        )
        with caplog.at_level(_logging.INFO):
            cfg = WorkflowConfig.load(tmp_path)
        assert cfg.max_fix_iterations == 42
        msgs = "\n".join(r.getMessage() for r in caplog.records)
        assert "max_fix_iterations=42" in msgs
        assert "se3.yaml" in msgs

    def test_load_logs_local_yaml_as_winning_source(self, tmp_path: Path, caplog):
        """When se3.local.yaml shadows se3.yaml, the log names the local file."""
        import logging as _logging

        # se3.local.yaml shadows se3.yaml as a whole (select-one, not key-merge).
        (tmp_path / "se3.yaml").write_text(
            "workflow:\n  max_fix_iterations: 30\n", encoding="utf-8"
        )
        (tmp_path / "se3.local.yaml").write_text(
            "workflow:\n  max_fix_iterations: 100\n", encoding="utf-8"
        )
        with caplog.at_level(_logging.INFO):
            cfg = WorkflowConfig.load(tmp_path)
        # The local file wins entirely.
        assert cfg.max_fix_iterations == 100
        msgs = "\n".join(r.getMessage() for r in caplog.records)
        assert "max_fix_iterations=100" in msgs
        assert "se3.local.yaml" in msgs

    def test_load_source_log_deduped_per_path(self, tmp_path: Path, caplog):
        """Repeated load() calls for the same config path log the source once."""
        import logging as _logging

        from se3 import config as _config

        # Isolate the module-level dedup set for this path.
        key = str((tmp_path / "se3.yaml").resolve())
        _config._logged_workflow_source_for.discard(key)

        (tmp_path / "se3.yaml").write_text(
            "workflow:\n  max_fix_iterations: 7\n", encoding="utf-8"
        )
        with caplog.at_level(_logging.INFO):
            WorkflowConfig.load(tmp_path)
            WorkflowConfig.load(tmp_path)
        source_lines = [
            r for r in caplog.records if "effective source" in r.getMessage()
        ]
        assert len(source_lines) == 1


# ---------------------------------------------------------------------------
# load_workflow_config
# ---------------------------------------------------------------------------

class TestLoadWorkflowConfig:
    def test_returns_workflow_config(self, tmp_path: Path):
        yaml_content = """
workflow:
  self_check_passes_required: 2
"""
        se3_yaml = tmp_path / "se3.yaml"
        se3_yaml.write_text(yaml_content, encoding="utf-8")

        cfg = load_workflow_config(tmp_path)
        assert isinstance(cfg, WorkflowConfig)
        assert cfg.self_check_passes_required == 2

    def test_none_project_root_uses_cwd(self, tmp_path: Path, monkeypatch):
        """When project_root is None, uses current working directory."""
        monkeypatch.chdir(tmp_path)
        cfg = load_workflow_config(None)
        assert isinstance(cfg, WorkflowConfig)
        assert cfg.self_check_passes_required == DEFAULT_SELF_CHECK_PASSES_REQUIRED


# ---------------------------------------------------------------------------
# get_max_fix_iterations (refactored)
# ---------------------------------------------------------------------------

class TestGetMaxFixIterations:
    def test_default_when_no_config(self, tmp_path: Path):
        assert get_max_fix_iterations(tmp_path) == DEFAULT_MAX_FIX_ITERATIONS

    def test_reads_from_workflow_section(self, tmp_path: Path):
        yaml_content = """
workflow:
  max_fix_iterations: 8
"""
        se3_yaml = tmp_path / "se3.yaml"
        se3_yaml.write_text(yaml_content, encoding="utf-8")

        assert get_max_fix_iterations(tmp_path) == 8

    def test_none_project_root_uses_cwd(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert get_max_fix_iterations() == DEFAULT_MAX_FIX_ITERATIONS

    def test_backward_compat_ignores_other_sections(self, tmp_path: Path):
        """Other sections do not interfere with max_fix_iterations reading."""
        yaml_content = """
version:
  enabled: false
workflow:
  max_fix_iterations: 12
"""
        se3_yaml = tmp_path / "se3.yaml"
        se3_yaml.write_text(yaml_content, encoding="utf-8")

        assert get_max_fix_iterations(tmp_path) == 12
