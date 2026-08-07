"""Tests for ``tianluo.config.E2EConfig`` — the tianluo.yaml ``e2e:`` block.

Every case writes its own YAML into ``tmp_path``; nothing reads the repository's
own ``tianluo.yaml``, so the suite stays valid however this project configures
itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tianluo.config import E2E_RUNTIME_CHOICES, E2EConfig


def write_config(root: Path, data: dict) -> Path:
    path = root / "tianluo.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def load(root: Path, e2e: object) -> E2EConfig:
    write_config(root, {"e2e": e2e})
    return E2EConfig.load(root)


class TestDefaults:
    def test_no_config_file_at_all(self, tmp_path):
        config = E2EConfig.load(tmp_path)
        assert config.enabled is False
        assert config.runtime == "auto"
        assert config.oci_runtime is None
        assert config.scenarios == []

    def test_config_without_e2e_block(self, tmp_path):
        write_config(tmp_path, {"test": {"timeout": 60}})
        assert E2EConfig.load(tmp_path).enabled is False

    def test_explicitly_disabled(self, tmp_path):
        assert load(tmp_path, {"enabled": False}).enabled is False

    def test_empty_block(self, tmp_path):
        assert load(tmp_path, {}).enabled is False

    def test_null_block(self, tmp_path):
        assert load(tmp_path, None).enabled is False

    def test_non_mapping_block_falls_back(self, tmp_path):
        config = load(tmp_path, ["enabled"])
        assert config.enabled is False
        assert config.runtime == "auto"

    def test_defaults_are_not_shared_between_instances(self, tmp_path):
        first = E2EConfig.load(tmp_path)
        first.scenarios.append("leaked")
        assert E2EConfig.load(tmp_path).scenarios == []


class TestEnabled:
    @pytest.mark.parametrize("raw", [True, "true", "yes", "on", 1])
    def test_truthy_forms(self, tmp_path, raw):
        assert load(tmp_path, {"enabled": raw}).enabled is True

    @pytest.mark.parametrize("raw", [False, "false", "no", "off", 0, "nonsense"])
    def test_falsy_and_unparseable_forms_stay_off(self, tmp_path, raw):
        assert load(tmp_path, {"enabled": raw}).enabled is False

    @pytest.mark.parametrize("raw", ["ture", "maybe", ["yes"]])
    def test_an_unrecognized_switch_warns_before_falling_back(
        self, tmp_path, raw, caplog
    ):
        """Silence here is the costly kind: the user believes e2e is running."""
        with caplog.at_level("WARNING"):
            config = load(tmp_path, {"enabled": raw})

        assert config.enabled is False
        assert "e2e.enabled" in caplog.text

    def test_an_unrecognized_keep_environment_warns_too(self, tmp_path, caplog):
        with caplog.at_level("WARNING"):
            config = load(tmp_path, {"keep_environment": "kinda"})

        assert config.keep_environment is False
        assert "e2e.keep_environment" in caplog.text


class TestRuntimeSelection:
    @pytest.mark.parametrize("value", E2E_RUNTIME_CHOICES)
    def test_valid_values_pass_through(self, tmp_path, value):
        assert load(tmp_path, {"runtime": value}).runtime == value

    def test_case_and_whitespace_normalized(self, tmp_path):
        assert load(tmp_path, {"runtime": "  Podman "}).runtime == "podman"

    def test_invalid_string_warns_and_falls_back_to_auto(self, tmp_path, caplog):
        with caplog.at_level("WARNING"):
            config = load(tmp_path, {"runtime": "containerd"})
        assert config.runtime == "auto"
        assert "e2e.runtime" in caplog.text

    def test_non_string_falls_back_without_raising(self, tmp_path):
        assert load(tmp_path, {"runtime": 7}).runtime == "auto"

    def test_null_falls_back(self, tmp_path):
        assert load(tmp_path, {"runtime": None}).runtime == "auto"


class TestOciRuntime:
    def test_string_kept_and_stripped(self, tmp_path):
        assert load(tmp_path, {"oci_runtime": " kata "}).oci_runtime == "kata"

    def test_absent_is_none(self, tmp_path):
        assert load(tmp_path, {"enabled": True}).oci_runtime is None

    @pytest.mark.parametrize("raw", ["", "   ", 5, ["kata"]])
    def test_malformed_falls_back_to_none(self, tmp_path, raw):
        assert load(tmp_path, {"oci_runtime": raw}).oci_runtime is None


class TestTimeouts:
    def test_values_pass_through(self, tmp_path):
        config = load(tmp_path, {"build_timeout": 60, "scenario_timeout": 30})
        assert config.build_timeout == 60
        assert config.scenario_timeout == 30

    @pytest.mark.parametrize("raw", [0, -1, "soon", None, [30], True, False])
    def test_non_positive_and_malformed_clamp_to_default(self, tmp_path, raw):
        """`build_timeout: yes` is a YAML bool, and int(True) is a positive 1 —
        a one-second build budget nobody asked for unless bools are refused."""
        config = load(tmp_path, {"build_timeout": raw, "scenario_timeout": raw})
        assert config.build_timeout == 1800
        assert config.scenario_timeout == 300

    def test_numeric_string_accepted(self, tmp_path):
        assert load(tmp_path, {"scenario_timeout": "45"}).scenario_timeout == 45

    def test_estimated_duration_absent_is_none(self, tmp_path):
        assert load(tmp_path, {"enabled": True}).estimated_e2e_duration is None

    def test_estimated_duration_value(self, tmp_path):
        config = load(tmp_path, {"estimated_e2e_duration": 900})
        assert config.estimated_e2e_duration == 900

    @pytest.mark.parametrize("raw", [0, -5, "later"])
    def test_estimated_duration_invalid_becomes_none(self, tmp_path, raw):
        assert load(tmp_path, {"estimated_e2e_duration": raw}).estimated_e2e_duration is None


class TestScenarioSelection:
    def test_lists_parsed(self, tmp_path):
        config = load(
            tmp_path,
            {"scenarios": ["login", "checkout"], "critical_scenarios": ["login"]},
        )
        assert config.scenarios == ["login", "checkout"]
        assert config.critical_scenarios == ["login"]

    def test_elements_coerced_to_strings(self, tmp_path):
        assert load(tmp_path, {"scenarios": [1, 2]}).scenarios == ["1", "2"]

    def test_none_entries_dropped(self, tmp_path):
        assert load(tmp_path, {"scenarios": ["a", None]}).scenarios == ["a"]

    def test_non_list_warns_and_empties(self, tmp_path, caplog):
        with caplog.at_level("WARNING"):
            config = load(tmp_path, {"scenarios": "login"})
        assert config.scenarios == []
        assert "e2e.scenarios" in caplog.text

    def test_empty_selection_selects_everything(self, tmp_path):
        config = load(tmp_path, {"enabled": True})
        assert config.selects("anything") is True

    def test_selection_filters(self, tmp_path):
        config = load(tmp_path, {"scenarios": ["login"]})
        assert config.selects("login") is True
        assert config.selects("checkout") is False


class TestKeepEnvironment:
    def test_default_off(self, tmp_path):
        assert load(tmp_path, {"enabled": True}).keep_environment is False

    def test_enabled(self, tmp_path):
        assert load(tmp_path, {"keep_environment": "true"}).keep_environment is True


class TestFullBlock:
    def test_all_fields_together(self, tmp_path):
        config = load(
            tmp_path,
            {
                "enabled": True,
                "runtime": "podman",
                "oci_runtime": "kata",
                "build_timeout": 900,
                "scenario_timeout": 120,
                "estimated_e2e_duration": 600,
                "scenarios": ["smoke"],
                "critical_scenarios": ["smoke"],
                "keep_environment": True,
            },
        )
        assert (config.enabled, config.runtime, config.oci_runtime) == (
            True, "podman", "kata",
        )
        assert (config.build_timeout, config.scenario_timeout) == (900, 120)
        assert config.estimated_e2e_duration == 600
        assert config.scenarios == ["smoke"]
        assert config.critical_scenarios == ["smoke"]
        assert config.keep_environment is True

    def test_one_bad_field_does_not_poison_the_others(self, tmp_path):
        config = load(
            tmp_path, {"enabled": True, "runtime": "lxc", "scenario_timeout": 42}
        )
        assert config.enabled is True
        assert config.runtime == "auto"
        assert config.scenario_timeout == 42

    @pytest.mark.parametrize("value", [float("inf"), float("nan"), 1e999])
    def test_a_non_finite_timeout_does_not_disable_e2e(self, tmp_path, value):
        """`int(inf)` raises OverflowError, which the field guard must absorb.

        Escaping it reaches ``load``'s blanket handler, which discards the whole
        block — turning one malformed timeout into a silent ``enabled: False``
        for a user who explicitly switched e2e on. A typo like ``1e999`` reaches
        the same place: YAML parses it as infinity.
        """
        config = load(
            tmp_path,
            {"enabled": True, "runtime": "podman", "build_timeout": value},
        )
        assert config.enabled is True
        assert config.runtime == "podman"
        assert config.build_timeout == 1800

    def test_malformed_yaml_file_yields_defaults(self, tmp_path):
        (tmp_path / "tianluo.yaml").write_text("e2e: [unclosed\n", encoding="utf-8")
        assert E2EConfig.load(tmp_path).enabled is False


def test_config_layer_does_not_import_the_e2e_package():
    """INVARIANT: config must not depend on tianluo.e2e.

    ``tianluo.e2e.content_config`` reads ``tianluo.config`` indirectly through the
    session layer; if the runtime-settings dataclass reached back into the e2e
    package the two would form an import cycle, and every ``import
    tianluo.config`` — which is essentially the whole CLI — would start dragging
    in the e2e subsystem.
    """
    import ast

    path = Path(__file__).resolve().parents[2] / "src" / "tianluo" / "config.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # Relative imports keep their leading dots so `from .e2e import x`
            # is caught alongside the absolute form.
            imported.append("." * node.level + (node.module or ""))
    offenders = [
        name
        for name in imported
        if name.split(".")[:2] == ["tianluo", "e2e"] or name.lstrip(".").startswith("e2e")
    ]
    assert offenders == [], f"config.py imports the e2e package: {offenders}"
