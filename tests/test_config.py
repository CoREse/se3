"""Tests for WorkflowConfig and related config utilities.

Covers:
- WorkflowConfig.from_dict defaults, validation, and coercion
- WorkflowConfig.load from tianluo.yaml
- load_workflow_config convenience function
- get_max_fix_iterations refactored to use WorkflowConfig
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tianluo.config import (
    ConfigError,
    DEFAULT_ADJUDICATE_PERIOD,
    DEFAULT_BASELINE_FIX_MAX_ATTEMPTS,
    DEFAULT_INVESTIGATION_MAX_ITERATIONS,
    DEFAULT_MAX_FIX_ITERATIONS,
    DEFAULT_PLAN_DECOMPOSITION,
    DEFAULT_PLAN_GRANULARITY,
    DEFAULT_SELF_CHECK_CONVERGENCE_ENABLED,
    DEFAULT_SELF_CHECK_PASSES_REQUIRED,
    InvestigationConfig,
    TestConfig,
    WorkflowConfig,
    get_max_fix_iterations,
    load_investigation_config,
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

    def test_deprecated_convergence_true_normalizes_false(self, caplog):
        import tianluo.config as _config

        # The deprecation guard is process-level; reset it so this test's
        # warning assertion holds regardless of test ordering.
        _config._convergence_deprecation_warned = False
        cfg = WorkflowConfig.from_dict(
            {"workflow": {"self_check_convergence_enabled": True}}
        )
        assert cfg.self_check_convergence_enabled is False
        assert "deprecated" in caplog.text

    def test_deprecated_convergence_warns_once_per_process(self, caplog):
        import logging as _logging

        import tianluo.config as _config

        # The deprecation guard is process-level; reset it so this test's
        # warning assertion holds regardless of test ordering.
        _config._convergence_deprecation_warned = False
        with caplog.at_level(_logging.WARNING):
            cfg = WorkflowConfig.from_dict(
                {"workflow": {"self_check_convergence_enabled": True}}
            )
            # Workflow config is loaded repeatedly during a flow; the
            # deprecation warning must be emitted once per process, not once
            # per parse.
            again = WorkflowConfig.from_dict(
                {"workflow": {"self_check_convergence_enabled": True}}
            )
        assert cfg.self_check_convergence_enabled is False
        assert again.self_check_convergence_enabled is False
        messages = [
            record.message for record in caplog.records
            if "self_check_convergence_enabled is deprecated" in record.message
        ]
        assert len(messages) == 1

    def test_deprecated_convergence_string_true_normalizes_false(self):
        cfg = WorkflowConfig.from_dict(
            {"workflow": {"self_check_convergence_enabled": "true"}}
        )
        assert cfg.self_check_convergence_enabled is False

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
        assert cfg.self_check_convergence_enabled is False

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

    # -- adjudicate_period (adjudicate step periodic safety-net trigger) --

    def test_adjudicate_period_default_is_10(self):
        assert DEFAULT_ADJUDICATE_PERIOD == 10
        assert WorkflowConfig().adjudicate_period == 10
        assert WorkflowConfig.from_dict({}).adjudicate_period == 10

    def test_custom_adjudicate_period(self):
        cfg = WorkflowConfig.from_dict({"workflow": {"adjudicate_period": 5}})
        assert cfg.adjudicate_period == 5

    def test_adjudicate_period_null_disables(self):
        """null/None normalizes to 0 (periodic safety net disabled)."""
        cfg = WorkflowConfig.from_dict({"workflow": {"adjudicate_period": None}})
        assert cfg.adjudicate_period == 0

    def test_adjudicate_period_zero_preserved(self):
        cfg = WorkflowConfig.from_dict({"workflow": {"adjudicate_period": 0}})
        assert cfg.adjudicate_period == 0

    def test_adjudicate_period_string_coercion(self):
        cfg = WorkflowConfig.from_dict({"workflow": {"adjudicate_period": "7"}})
        assert cfg.adjudicate_period == 7

    def test_adjudicate_period_negative_raises(self):
        with pytest.raises(ConfigError, match="must be >= 0"):
            WorkflowConfig.from_dict({"workflow": {"adjudicate_period": -1}})

    def test_adjudicate_period_invalid_raises(self):
        # A malformed type must fail configuration validation rather than
        # silently enabling the default periodic ADJUDICATE interval.
        with pytest.raises(ConfigError, match="must be an integer"):
            WorkflowConfig.from_dict(
                {"workflow": {"adjudicate_period": "not_a_number"}}
            )

    def test_adjudicate_period_bool_raises(self):
        with pytest.raises(ConfigError, match="must be an integer"):
            WorkflowConfig.from_dict({"workflow": {"adjudicate_period": True}})

    def test_adjudicate_period_float_raises(self):
        with pytest.raises(ConfigError, match="must be an integer"):
            WorkflowConfig.from_dict({"workflow": {"adjudicate_period": 3.0}})

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
# WorkflowConfig.plan_decomposition / plan_granularity
# ---------------------------------------------------------------------------


@pytest.fixture
def reset_strategy_deprecation_warning(monkeypatch):
    """The deprecation guard is per-process; isolate it per test."""
    import tianluo.config as config_mod

    monkeypatch.setattr(
        config_mod, "_implementation_strategy_deprecation_warned", False
    )
    return config_mod


class TestPlanDecompositionAndGranularity:
    @pytest.mark.parametrize("value", ["capability", "granular"])
    def test_plan_decomposition_accepts_all_legal_values(self, value):
        cfg = WorkflowConfig.from_dict({"workflow": {"plan_decomposition": value}})
        assert cfg.plan_decomposition == value
        assert cfg.plan_decomposition_explicit is True

    @pytest.mark.parametrize("value", ["auto", "single", "conservative"])
    def test_plan_granularity_accepts_all_legal_values(self, value):
        cfg = WorkflowConfig.from_dict({"workflow": {"plan_granularity": value}})
        assert cfg.plan_granularity == value
        assert cfg.plan_granularity_explicit is True

    def test_defaults_are_capability_and_auto(self):
        cfg = WorkflowConfig.from_dict({})
        assert cfg.plan_decomposition == DEFAULT_PLAN_DECOMPOSITION == "capability"
        assert cfg.plan_granularity == DEFAULT_PLAN_GRANULARITY == "auto"
        assert cfg.plan_decomposition_explicit is False
        assert cfg.plan_granularity_explicit is False

    def test_dataclass_defaults_match_module_defaults(self):
        cfg = WorkflowConfig()
        assert cfg.plan_decomposition == "capability"
        assert cfg.plan_granularity == "auto"

    @pytest.mark.parametrize("value", ["coarse", "CAPABILITY", "", None, 1])
    def test_invalid_plan_decomposition_names_config_path(self, value):
        with pytest.raises(
            ConfigError,
            match=r"workflow\.plan_decomposition=.*must be one of",
        ) as exc:
            WorkflowConfig.from_dict({"workflow": {"plan_decomposition": value}})
        assert "capability" in str(exc.value)
        assert "granular" in str(exc.value)

    @pytest.mark.parametrize("value", ["coarsest", "AUTO", "", None, 1])
    def test_invalid_plan_granularity_names_config_path(self, value):
        with pytest.raises(
            ConfigError,
            match=r"workflow\.plan_granularity=.*must be one of",
        ) as exc:
            WorkflowConfig.from_dict({"workflow": {"plan_granularity": value}})
        assert "conservative" in str(exc.value)


class TestRetiredImplementationStrategyMapping:
    """``workflow.implementation_strategy`` is mapped for one version."""

    @pytest.mark.parametrize(
        "strategy,decomposition,granularity",
        [
            ("direct", "capability", "single"),
            ("planned", "granular", "auto"),
            ("auto", "capability", "auto"),
        ],
    )
    def test_legacy_values_map_onto_new_keys(
        self, reset_strategy_deprecation_warning, strategy, decomposition, granularity
    ):
        cfg = WorkflowConfig.from_dict(
            {"workflow": {"implementation_strategy": strategy}}
        )
        assert cfg.plan_decomposition == decomposition
        assert cfg.plan_granularity == granularity

    def test_explicit_new_keys_beat_the_legacy_key(
        self, reset_strategy_deprecation_warning
    ):
        cfg = WorkflowConfig.from_dict(
            {
                "workflow": {
                    "implementation_strategy": "planned",
                    "plan_decomposition": "capability",
                    "plan_granularity": "conservative",
                }
            }
        )
        assert cfg.plan_decomposition == "capability"
        assert cfg.plan_granularity == "conservative"

    def test_mapping_warns_once_per_process(
        self, reset_strategy_deprecation_warning, caplog
    ):
        with caplog.at_level("WARNING", logger="tianluo.config"):
            WorkflowConfig.from_dict({"workflow": {"implementation_strategy": "direct"}})
            WorkflowConfig.from_dict({"workflow": {"implementation_strategy": "direct"}})

        warnings = [
            r
            for r in caplog.records
            if "implementation_strategy is deprecated" in r.getMessage()
        ]
        assert len(warnings) == 1

    @pytest.mark.parametrize("value", ["automatic", "DIRECT", "", None, 1])
    def test_invalid_legacy_value_still_fails_fast(
        self, reset_strategy_deprecation_warning, value
    ):
        with pytest.raises(
            ConfigError,
            match=r"workflow\.implementation_strategy=.*must be one of",
        ):
            WorkflowConfig.from_dict({"workflow": {"implementation_strategy": value}})

    def test_retired_api_surface_is_gone(self):
        cfg = WorkflowConfig()
        assert not hasattr(cfg, "implementation_strategy")
        assert not hasattr(cfg, "implementation_strategy_explicit")
        assert not hasattr(cfg, "resolve_implementation_strategy")


# ---------------------------------------------------------------------------
# WorkflowConfig.baseline_fix_max_attempts (mechanism B)
# ---------------------------------------------------------------------------

class TestBaselineFixMaxAttempts:
    def test_default_value_is_3(self):
        assert DEFAULT_BASELINE_FIX_MAX_ATTEMPTS == 3
        assert WorkflowConfig().baseline_fix_max_attempts == 3

    def test_default_when_workflow_section_missing(self):
        cfg = WorkflowConfig.from_dict({})
        assert cfg.baseline_fix_max_attempts == DEFAULT_BASELINE_FIX_MAX_ATTEMPTS

    def test_custom_value(self):
        cfg = WorkflowConfig.from_dict({"workflow": {"baseline_fix_max_attempts": 5}})
        assert cfg.baseline_fix_max_attempts == 5

    def test_zero_disables_baseline_loop(self):
        """0 is a valid value meaning 'do not loop baseline failures'."""
        cfg = WorkflowConfig.from_dict({"workflow": {"baseline_fix_max_attempts": 0}})
        assert cfg.baseline_fix_max_attempts == 0

    def test_string_coercion(self):
        cfg = WorkflowConfig.from_dict({"workflow": {"baseline_fix_max_attempts": "4"}})
        assert cfg.baseline_fix_max_attempts == 4

    def test_negative_raises(self):
        with pytest.raises(ConfigError, match="must be >= 0"):
            WorkflowConfig.from_dict({"workflow": {"baseline_fix_max_attempts": -1}})

    def test_negative_string_raises(self):
        with pytest.raises(ConfigError, match="must be >= 0"):
            WorkflowConfig.from_dict({"workflow": {"baseline_fix_max_attempts": "-2"}})

    def test_bool_warns_and_falls_back(self):
        cfg = WorkflowConfig.from_dict({"workflow": {"baseline_fix_max_attempts": True}})
        assert cfg.baseline_fix_max_attempts == DEFAULT_BASELINE_FIX_MAX_ATTEMPTS
        cfg = WorkflowConfig.from_dict({"workflow": {"baseline_fix_max_attempts": False}})
        assert cfg.baseline_fix_max_attempts == DEFAULT_BASELINE_FIX_MAX_ATTEMPTS

    def test_float_warns_and_falls_back(self):
        cfg = WorkflowConfig.from_dict({"workflow": {"baseline_fix_max_attempts": 2.0}})
        assert cfg.baseline_fix_max_attempts == DEFAULT_BASELINE_FIX_MAX_ATTEMPTS

    def test_non_numeric_string_falls_back(self):
        cfg = WorkflowConfig.from_dict(
            {"workflow": {"baseline_fix_max_attempts": "not_a_number"}}
        )
        assert cfg.baseline_fix_max_attempts == DEFAULT_BASELINE_FIX_MAX_ATTEMPTS


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
        se3_yaml = tmp_path / "tianluo.yaml"
        se3_yaml.write_text(yaml_content, encoding="utf-8")

        cfg = WorkflowConfig.load(tmp_path)
        assert cfg.max_fix_iterations == 10
        assert cfg.self_check_passes_required == 3
        assert cfg.self_check_convergence_enabled is False

    def test_load_no_config_file(self, tmp_path: Path):
        """When no tianluo.yaml exists, defaults are returned."""
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
        se3_yaml = tmp_path / "tianluo.yaml"
        se3_yaml.write_text(yaml_content, encoding="utf-8")

        with pytest.raises(ConfigError, match="must be >= 1"):
            WorkflowConfig.load(tmp_path)

    def test_load_logs_effective_source_se3_yaml(self, tmp_path: Path, caplog):
        """load() logs which file the resolved max_fix_iterations came from."""
        import logging as _logging

        (tmp_path / "tianluo.yaml").write_text(
            "workflow:\n  max_fix_iterations: 42\n", encoding="utf-8"
        )
        with caplog.at_level(_logging.INFO):
            cfg = WorkflowConfig.load(tmp_path)
        assert cfg.max_fix_iterations == 42
        msgs = "\n".join(r.getMessage() for r in caplog.records)
        assert "max_fix_iterations=42" in msgs
        assert "tianluo.yaml" in msgs

    def test_load_logs_local_yaml_as_winning_source(self, tmp_path: Path, caplog):
        """When tianluo.local.yaml shadows tianluo.yaml, the log names the local file."""
        import logging as _logging

        # tianluo.local.yaml shadows tianluo.yaml as a whole (select-one, not key-merge).
        (tmp_path / "tianluo.yaml").write_text(
            "workflow:\n  max_fix_iterations: 30\n", encoding="utf-8"
        )
        (tmp_path / "tianluo.local.yaml").write_text(
            "workflow:\n  max_fix_iterations: 100\n", encoding="utf-8"
        )
        with caplog.at_level(_logging.INFO):
            cfg = WorkflowConfig.load(tmp_path)
        # The local file wins entirely.
        assert cfg.max_fix_iterations == 100
        msgs = "\n".join(r.getMessage() for r in caplog.records)
        assert "max_fix_iterations=100" in msgs
        assert "tianluo.local.yaml" in msgs

    def test_load_source_log_deduped_per_path(self, tmp_path: Path, caplog):
        """Repeated load() calls for the same config path log the source once."""
        import logging as _logging

        from tianluo import config as _config

        # Isolate the module-level dedup set for this path.
        key = str((tmp_path / "tianluo.yaml").resolve())
        _config._logged_workflow_source_for.discard(key)

        (tmp_path / "tianluo.yaml").write_text(
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
        se3_yaml = tmp_path / "tianluo.yaml"
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
        se3_yaml = tmp_path / "tianluo.yaml"
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
        se3_yaml = tmp_path / "tianluo.yaml"
        se3_yaml.write_text(yaml_content, encoding="utf-8")

        assert get_max_fix_iterations(tmp_path) == 12


# ---------------------------------------------------------------------------
# InvestigationConfig — the investigate step's own bounded loop
# ---------------------------------------------------------------------------

class TestInvestigationConfig:
    def test_default_is_three_rounds(self):
        assert InvestigationConfig().max_iterations == 3
        assert DEFAULT_INVESTIGATION_MAX_ITERATIONS == 3

    def test_missing_section_returns_default(self):
        cfg = InvestigationConfig.from_dict({"workflow": {"max_fix_iterations": 5}})
        assert cfg.max_iterations == DEFAULT_INVESTIGATION_MAX_ITERATIONS

    def test_empty_dict_returns_default(self):
        assert InvestigationConfig.from_dict({}).max_iterations == 3
        assert InvestigationConfig.from_dict(None).max_iterations == 3

    def test_custom_value(self):
        cfg = InvestigationConfig.from_dict({"investigation": {"max_iterations": 7}})
        assert cfg.max_iterations == 7

    def test_zero_is_the_unlimited_sentinel(self):
        cfg = InvestigationConfig.from_dict({"investigation": {"max_iterations": 0}})
        assert cfg.max_iterations == 0

    def test_null_normalizes_to_the_unlimited_sentinel(self):
        cfg = InvestigationConfig.from_dict({"investigation": {"max_iterations": None}})
        assert cfg.max_iterations == 0

    def test_negative_raises(self):
        with pytest.raises(ConfigError, match="must be >= 0"):
            InvestigationConfig.from_dict({"investigation": {"max_iterations": -1}})

    def test_float_warns_and_falls_back(self, caplog):
        cfg = InvestigationConfig.from_dict({"investigation": {"max_iterations": 3.0}})
        assert cfg.max_iterations == DEFAULT_INVESTIGATION_MAX_ITERATIONS
        assert "not an integer" in caplog.text or "not a valid integer" in caplog.text

    def test_bool_warns_and_falls_back(self, caplog):
        cfg = InvestigationConfig.from_dict({"investigation": {"max_iterations": True}})
        assert cfg.max_iterations == DEFAULT_INVESTIGATION_MAX_ITERATIONS
        assert "not a valid integer" in caplog.text

    def test_non_numeric_string_falls_back(self):
        cfg = InvestigationConfig.from_dict(
            {"investigation": {"max_iterations": "not_a_number"}}
        )
        assert cfg.max_iterations == DEFAULT_INVESTIGATION_MAX_ITERATIONS

    def test_string_integer_is_coerced(self):
        cfg = InvestigationConfig.from_dict({"investigation": {"max_iterations": "5"}})
        assert cfg.max_iterations == 5

    def test_non_dict_section_returns_default(self):
        cfg = InvestigationConfig.from_dict({"investigation": "nope"})
        assert cfg.max_iterations == DEFAULT_INVESTIGATION_MAX_ITERATIONS


class TestLoadInvestigationConfig:
    def test_no_yaml_returns_defaults(self, tmp_path: Path):
        assert load_investigation_config(tmp_path).max_iterations == 3

    def test_loads_from_project_yaml(self, tmp_path: Path):
        (tmp_path / "tianluo.yaml").write_text(
            "investigation:\n  max_iterations: 5\n", encoding="utf-8"
        )
        assert load_investigation_config(tmp_path).max_iterations == 5

    def test_zero_sentinel_from_yaml(self, tmp_path: Path):
        (tmp_path / "tianluo.yaml").write_text(
            "investigation:\n  max_iterations: 0\n", encoding="utf-8"
        )
        assert load_investigation_config(tmp_path).max_iterations == 0

    def test_none_project_root_uses_cwd(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert load_investigation_config().max_iterations == 3

    def test_example_yaml_documents_the_section(self):
        text = (
            Path(__file__).parent.parent / "tianluo.example.yaml"
        ).read_text(encoding="utf-8")
        assert "investigation:" in text
        assert "max_iterations" in text
        # The 0 sentinel must be documented, not just the default value.
        assert "无上限" in text
