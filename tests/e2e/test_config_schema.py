"""Tests for ``tianluo.e2e.config_schema`` and ``tianluo.e2e.content_config``.

The validator is a pure function, so most cases build raw dicts directly; the
loader cases write real files under ``tmp_path``. Nothing here touches a
container runtime or the repository's own configuration.

Assertions match on the *located prefix* (``<file>: <yaml path>:``) rather than
on message wording, so the checks stay valid in any UI language.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tianluo.e2e import config_schema, content_config
from tianluo.e2e.config_schema import (
    BASE_KINDS,
    READINESS_KINDS,
    validate_content,
    validate_environment,
    validate_scenario,
)
from tianluo.e2e.content_config import (
    ENVIRONMENT_FILENAME,
    load_content_config,
    read_raw_content,
)
from tianluo.e2e.errors import E2EConfigError

ENV_SOURCE = "tianluo/e2e/environment.yaml"
SCENARIO_SOURCE = "tianluo/e2e/scenarios/smoke.yaml"


def environment(**overrides) -> dict:
    """A minimal valid two-service topology."""
    data = {
        "services": [
            {
                "name": "app",
                "image": "python:3.12-slim",
                "base_kind": "base",
                "build": ["pip install -e ."],
                "ports": ["8000:8000"],
                "environment": {"APP_ENV": "test"},
                "readiness": {"kind": "http", "url": "http://app:8000/healthz"},
            },
            {
                "name": "db",
                "image": "postgres:16",
                "mount_source": False,
                "readiness": {"kind": "tcp", "port": 5432},
            },
        ]
    }
    data.update(overrides)
    return data


def scenario(**overrides) -> dict:
    """A minimal valid tier-1 scenario."""
    data = {
        "name": "smoke",
        "driver": "app",
        "actions": [{"action": "exec", "command": ["luo", "--version"]}],
        "assertions": [{"kind": "exit_code", "equals": 0}],
    }
    data.update(overrides)
    return data


def bundle(env=None, scenarios=None) -> dict:
    return {
        "environment": environment() if env is None else env,
        "environment_source": ENV_SOURCE,
        "scenarios": {SCENARIO_SOURCE: scenario()} if scenarios is None else scenarios,
    }


def located(errors, source: str, path: str) -> list[str]:
    """Errors anchored at exactly ``source``/``path``."""
    return [e for e in errors if e.startswith(f"{source}: {path}: ")]


# --------------------------------------------------------------------------
# Positive baseline
# --------------------------------------------------------------------------


class TestValidContent:
    def test_minimal_bundle_has_no_errors(self):
        assert validate_content(bundle(), "tianluo/e2e") == []

    def test_single_service_topology_is_valid(self):
        env = {"services": [{"name": "cli", "image": "debian:stable-slim"}]}
        scenarios = {SCENARIO_SOURCE: scenario(driver="cli")}
        assert validate_content(bundle(env, scenarios), "tianluo/e2e") == []

    @pytest.mark.parametrize("kind", BASE_KINDS)
    def test_every_base_kind_is_accepted(self, kind):
        env = environment()
        env["services"][0]["base_kind"] = kind
        errors, names = validate_environment(env, ENV_SOURCE)
        assert errors == []
        assert names == ["app", "db"]

    @pytest.mark.parametrize(
        "probe",
        [
            {"kind": "command", "command": ["true"]},
            {"kind": "http", "url": "http://app:8000/"},
            {"kind": "tcp", "port": 5432},
            {"kind": "log", "pattern": "listening on"},
        ],
    )
    def test_every_readiness_kind_is_accepted(self, probe):
        env = environment()
        env["services"][0]["readiness"] = probe
        assert validate_environment(env, ENV_SOURCE)[0] == []

    @pytest.mark.parametrize(
        "assertion",
        [
            {"kind": "exit_code", "equals": 0},
            {"kind": "stdout", "matches": r"^ok$"},
            {"kind": "stderr", "contains": "warning"},
            {"kind": "http_status", "url": "http://app:8000/", "equals": 200},
            {"kind": "http_body", "url": "http://app:8000/", "contains": "hello"},
            {"kind": "file_exists", "path": "/workspace/out.txt"},
            {"kind": "file_content", "path": "/workspace/out.txt", "matches": "ok"},
            {"kind": "dom", "selector": "#login-form"},
        ],
    )
    def test_every_tier1_assertion_is_accepted_without_declaration(self, assertion):
        """Tier 1 is the default: it needs no opt-in flag at all."""
        errors = validate_scenario(
            scenario(assertions=[assertion]),
            SCENARIO_SOURCE,
            service_names=["app", "db"],
            environment_source=ENV_SOURCE,
        )
        assert errors == []

    @pytest.mark.parametrize(
        "action",
        [
            {"action": "exec", "command": ["true"]},
            {"action": "http", "url": "http://app:8000/"},
            {"action": "browser", "op": "goto", "url": "http://app:8000/"},
            {"action": "wait", "seconds": 1},
            {"action": "screenshot", "name": "home.png"},
        ],
    )
    def test_every_programmatic_action_is_accepted(self, action):
        errors = validate_scenario(
            scenario(actions=[action]),
            SCENARIO_SOURCE,
            service_names=["app"],
            environment_source=ENV_SOURCE,
        )
        assert errors == []


# --------------------------------------------------------------------------
# environment.yaml rules
# --------------------------------------------------------------------------


class TestEnvironmentRules:
    def test_non_mapping_document(self):
        errors, names = validate_environment(["services"], ENV_SOURCE)
        assert located(errors, ENV_SOURCE, "<root>")
        assert names == []

    def test_services_missing(self):
        errors, _ = validate_environment({}, ENV_SOURCE)
        assert located(errors, ENV_SOURCE, "services")

    def test_services_not_a_list(self):
        errors, _ = validate_environment({"services": {"app": {}}}, ENV_SOURCE)
        assert located(errors, ENV_SOURCE, "services")

    def test_services_empty(self):
        errors, _ = validate_environment({"services": []}, ENV_SOURCE)
        assert located(errors, ENV_SOURCE, "services")

    def test_service_entry_not_a_mapping(self):
        errors, _ = validate_environment({"services": ["app"]}, ENV_SOURCE)
        assert located(errors, ENV_SOURCE, "services[0]")

    def test_service_name_missing(self):
        errors, _ = validate_environment(
            {"services": [{"image": "python:3.12-slim"}]}, ENV_SOURCE
        )
        assert located(errors, ENV_SOURCE, "services[0].name")

    @pytest.mark.parametrize(
        "name", ["App", "my_app", "-app", "app-", "a" * 64, "app.db", "app service"]
    )
    def test_service_name_must_be_a_dns_label(self, name):
        env = {"services": [{"name": name, "image": "x"}]}
        errors, names = validate_environment(env, ENV_SOURCE)
        assert located(errors, ENV_SOURCE, "services[0].name")
        assert names == []

    def test_service_names_must_be_unique(self):
        env = {
            "services": [
                {"name": "app", "image": "x"},
                {"name": "app", "image": "y"},
            ]
        }
        errors, names = validate_environment(env, ENV_SOURCE)
        assert located(errors, ENV_SOURCE, "services[1].name")
        assert names == ["app"]

    def test_image_required(self):
        errors, _ = validate_environment({"services": [{"name": "app"}]}, ENV_SOURCE)
        assert located(errors, ENV_SOURCE, "services[0].image")

    def test_unknown_base_kind(self):
        env = environment()
        env["services"][0]["base_kind"] = "alpine-custom"
        errors, _ = validate_environment(env, ENV_SOURCE)
        assert located(errors, ENV_SOURCE, "services[0].base_kind")

    def test_build_steps_must_be_a_list(self):
        env = environment()
        env["services"][0]["build"] = "pip install -e ."
        errors, _ = validate_environment(env, ENV_SOURCE)
        assert located(errors, ENV_SOURCE, "services[0].build")

    def test_build_step_must_be_a_string(self):
        env = environment()
        env["services"][0]["build"] = [{"run": "pip install"}]
        errors, _ = validate_environment(env, ENV_SOURCE)
        assert located(errors, ENV_SOURCE, "services[0].build[0]")

    def test_unknown_readiness_kind(self):
        env = environment()
        env["services"][0]["readiness"] = {"kind": "smoke-signal"}
        errors, _ = validate_environment(env, ENV_SOURCE)
        assert located(errors, ENV_SOURCE, "services[0].readiness.kind")

    @pytest.mark.parametrize(
        "probe,missing",
        [
            ({"kind": "command"}, "command"),
            ({"kind": "http"}, "url"),
            ({"kind": "tcp"}, "port"),
            ({"kind": "log"}, "pattern"),
        ],
    )
    def test_readiness_requires_its_companion_field(self, probe, missing):
        env = environment()
        env["services"][0]["readiness"] = probe
        errors, _ = validate_environment(env, ENV_SOURCE)
        assert located(errors, ENV_SOURCE, f"services[0].readiness.{missing}")

    @pytest.mark.parametrize("budget", ["timeout", "interval"])
    def test_readiness_budgets_must_be_positive(self, budget):
        env = environment()
        env["services"][0]["readiness"] = {
            "kind": "http", "url": "http://app/", budget: 0
        }
        errors, _ = validate_environment(env, ENV_SOURCE)
        assert located(errors, ENV_SOURCE, f"services[0].readiness.{budget}")

    def test_environment_map_must_be_a_mapping(self):
        env = environment()
        env["services"][0]["environment"] = ["APP_ENV=test"]
        errors, _ = validate_environment(env, ENV_SOURCE)
        assert located(errors, ENV_SOURCE, "services[0].environment")

    def test_network_name_must_be_a_dns_label(self):
        errors, _ = validate_environment(environment(network="My Net"), ENV_SOURCE)
        assert located(errors, ENV_SOURCE, "network")

    def test_valid_network_name_accepted(self):
        assert validate_environment(environment(network="e2e-net"), ENV_SOURCE)[0] == []

    def test_readiness_kinds_are_exactly_the_documented_four(self):
        assert set(READINESS_KINDS) == {"command", "http", "tcp", "log"}


# --------------------------------------------------------------------------
# scenario rules
# --------------------------------------------------------------------------


class TestScenarioRules:
    def validate(self, data, *, services=("app", "db"), **kwargs):
        return validate_scenario(
            data,
            SCENARIO_SOURCE,
            service_names=services,
            environment_source=ENV_SOURCE,
            **kwargs,
        )

    def test_name_required(self):
        data = scenario()
        del data["name"]
        assert located(self.validate(data), SCENARIO_SOURCE, "name")

    def test_driver_required(self):
        data = scenario()
        del data["driver"]
        assert located(self.validate(data), SCENARIO_SOURCE, "driver")

    def test_driver_must_name_a_declared_service(self):
        errors = self.validate(scenario(driver="browser"))
        anchored = located(errors, SCENARIO_SOURCE, "driver")
        assert anchored, errors
        # The message must locate both ends of the mismatch: the scenario file
        # it lives in and the environment file it should have matched.
        assert "browser" in anchored[0]
        assert ENV_SOURCE in anchored[0]

    def test_driver_accepted_when_declared(self):
        assert self.validate(scenario(driver="db")) == []

    def test_timeout_must_be_positive(self):
        assert located(self.validate(scenario(timeout=0)), SCENARIO_SOURCE, "timeout")

    def test_positive_timeout_accepted(self):
        assert self.validate(scenario(timeout=30)) == []

    def test_assertions_required(self):
        data = scenario()
        del data["assertions"]
        assert located(self.validate(data), SCENARIO_SOURCE, "assertions")

    def test_empty_assertions_rejected(self):
        assert located(self.validate(scenario(assertions=[])), SCENARIO_SOURCE, "assertions")

    def test_unknown_assertion_kind(self):
        errors = self.validate(scenario(assertions=[{"kind": "vibes"}]))
        assert located(errors, SCENARIO_SOURCE, "assertions[0].kind")

    def test_unknown_action_kind(self):
        errors = self.validate(scenario(actions=[{"action": "telepathy"}]))
        assert located(errors, SCENARIO_SOURCE, "actions[0].action")

    def test_action_missing_required_field(self):
        errors = self.validate(scenario(actions=[{"action": "exec"}]))
        assert located(errors, SCENARIO_SOURCE, "actions[0].command")

    def test_action_needing_one_of_several_fields(self):
        errors = self.validate(scenario(actions=[{"action": "wait"}]))
        assert located(errors, SCENARIO_SOURCE, "actions[0]")

    def test_assertion_missing_required_field(self):
        errors = self.validate(scenario(assertions=[{"kind": "dom"}]))
        assert located(errors, SCENARIO_SOURCE, "assertions[0].selector")

    def test_assertion_needing_one_of_several_fields(self):
        errors = self.validate(scenario(assertions=[{"kind": "stdout"}]))
        assert located(errors, SCENARIO_SOURCE, "assertions[0]")


# --------------------------------------------------------------------------
# Assertion ladder — the hard rules
# --------------------------------------------------------------------------


class TestAssertionLadder:
    """INVARIANT coverage: escalating a tier must be declared and justified."""

    def validate(self, assertion, **scenario_kwargs):
        return validate_scenario(
            scenario(assertions=[assertion], **scenario_kwargs),
            SCENARIO_SOURCE,
            service_names=["app"],
            environment_source=ENV_SOURCE,
        )

    # -- tier 2 -----------------------------------------------------------

    def test_screenshot_diff_without_visual_regression_flag_is_rejected(self):
        errors = self.validate({"kind": "screenshot_diff", "baseline": "home.png"})
        assert located(errors, SCENARIO_SOURCE, "assertions[0]")
        assert len(errors) == 1

    def test_screenshot_diff_with_declaration_is_accepted(self):
        assert self.validate(
            {
                "kind": "screenshot_diff",
                "baseline": "home.png",
                "visual_regression": True,
            }
        ) == []

    def test_screenshot_diff_requires_a_baseline(self):
        errors = self.validate(
            {"kind": "screenshot_diff", "visual_regression": True}
        )
        assert located(errors, SCENARIO_SOURCE, "assertions[0].baseline")

    def test_screenshot_diff_threshold_must_be_a_unit_fraction(self):
        errors = self.validate(
            {
                "kind": "screenshot_diff",
                "baseline": "home.png",
                "visual_regression": True,
                "threshold": 5,
            }
        )
        assert located(errors, SCENARIO_SOURCE, "assertions[0].threshold")

    def test_screenshot_diff_may_scope_to_a_selector(self):
        """A selector scopes the diff to one rendered region — still tier 2."""
        assert self.validate(
            {
                "kind": "screenshot_diff",
                "baseline": "home.png",
                "visual_regression": True,
                "selector": "#hero",
            }
        ) == []

    # -- tier 3 -----------------------------------------------------------

    def test_llm_vision_without_semantic_visual_flag_is_rejected(self):
        errors = self.validate(
            {"kind": "visual_semantic", "question": "Is the chart readable?",
             "require_evidence": True}
        )
        assert located(errors, SCENARIO_SOURCE, "assertions[0]")
        assert len(errors) == 1

    def test_llm_vision_without_evidence_requirement_is_rejected(self):
        errors = self.validate(
            {"kind": "visual_semantic", "question": "Is the chart readable?",
             "semantic_visual": True}
        )
        assert located(errors, SCENARIO_SOURCE, "assertions[0]")
        assert len(errors) == 1

    def test_fully_declared_llm_vision_is_accepted(self):
        assert self.validate(
            {
                "kind": "visual_semantic",
                "question": "Is the chart readable?",
                "semantic_visual": True,
                "require_evidence": True,
            }
        ) == []

    # -- escalation when a lower tier would do -----------------------------

    def test_llm_vision_with_a_selector_is_rejected_as_a_needless_escalation(self):
        """A selector proves a DOM assertion (tier 1) could answer this."""
        errors = self.validate(
            {
                "kind": "visual_semantic",
                "question": "Is the banner shown?",
                "semantic_visual": True,
                "require_evidence": True,
                "selector": "#banner",
            }
        )
        assert located(errors, SCENARIO_SOURCE, "assertions[0]")
        assert len(errors) == 1

    def test_screenshot_diff_with_a_text_expectation_is_rejected(self):
        errors = self.validate(
            {
                "kind": "screenshot_diff",
                "baseline": "home.png",
                "visual_regression": True,
                "text": "Welcome",
            }
        )
        assert located(errors, SCENARIO_SOURCE, "assertions[0]")
        assert len(errors) == 1

    # -- flags in the wrong place -----------------------------------------

    @pytest.mark.parametrize("flag", ["visual_regression", "semantic_visual"])
    def test_tier_flag_on_a_deterministic_assertion_is_rejected(self, flag):
        errors = self.validate({"kind": "exit_code", "equals": 0, flag: True})
        assert located(errors, SCENARIO_SOURCE, "assertions[0]")
        assert len(errors) == 1

    def test_semantic_flag_on_a_tier2_assertion_is_rejected(self):
        errors = self.validate(
            {
                "kind": "screenshot_diff",
                "baseline": "home.png",
                "visual_regression": True,
                "semantic_visual": True,
            }
        )
        assert located(errors, SCENARIO_SOURCE, "assertions[0]")
        assert len(errors) == 1

    # -- driving side -----------------------------------------------------

    def test_coordinate_click_without_visual_driving_is_rejected(self):
        errors = validate_scenario(
            scenario(actions=[{"action": "visual_click", "x": 10, "y": 20}]),
            SCENARIO_SOURCE,
            service_names=["app"],
            environment_source=ENV_SOURCE,
        )
        assert located(errors, SCENARIO_SOURCE, "actions[0]")
        assert len(errors) == 1

    def test_coordinate_click_with_visual_driving_declared_is_accepted(self):
        errors = validate_scenario(
            scenario(
                visual_driving=True,
                actions=[{"action": "visual_click", "x": 10, "y": 20}],
            ),
            SCENARIO_SOURCE,
            service_names=["app"],
            environment_source=ENV_SOURCE,
        )
        assert errors == []

    def test_coordinate_click_with_a_selector_is_rejected(self):
        errors = validate_scenario(
            scenario(
                visual_driving=True,
                actions=[
                    {"action": "visual_click", "x": 1, "y": 2, "selector": "#go"}
                ],
            ),
            SCENARIO_SOURCE,
            service_names=["app"],
            environment_source=ENV_SOURCE,
        )
        assert located(errors, SCENARIO_SOURCE, "actions[0]")
        assert len(errors) == 1


# --------------------------------------------------------------------------
# Baselines and bundle-level rules
# --------------------------------------------------------------------------


class TestBaselinesAndBundle:
    def test_baseline_check_is_skipped_without_a_directory(self):
        """Purity guarantee: no IO at all unless the caller opts in."""
        errors = validate_scenario(
            scenario(
                assertions=[
                    {
                        "kind": "screenshot_diff",
                        "baseline": "nope.png",
                        "visual_regression": True,
                    }
                ]
            ),
            SCENARIO_SOURCE,
            service_names=["app"],
            environment_source=ENV_SOURCE,
        )
        assert errors == []

    def test_missing_baseline_file_is_reported(self, tmp_path):
        errors = validate_scenario(
            scenario(
                assertions=[
                    {
                        "kind": "screenshot_diff",
                        "baseline": "home.png",
                        "visual_regression": True,
                    }
                ]
            ),
            SCENARIO_SOURCE,
            service_names=["app"],
            environment_source=ENV_SOURCE,
            baselines_dir=tmp_path,
        )
        anchored = located(errors, SCENARIO_SOURCE, "assertions[0].baseline")
        assert anchored
        assert "home.png" in anchored[0]

    def test_present_baseline_file_passes(self, tmp_path):
        (tmp_path / "home.png").write_bytes(b"\x89PNG")
        errors = validate_scenario(
            scenario(
                assertions=[
                    {
                        "kind": "screenshot_diff",
                        "baseline": "home.png",
                        "visual_regression": True,
                    }
                ]
            ),
            SCENARIO_SOURCE,
            service_names=["app"],
            environment_source=ENV_SOURCE,
            baselines_dir=tmp_path,
        )
        assert errors == []

    def test_duplicate_scenario_names_across_files_are_reported(self):
        other = "tianluo/e2e/scenarios/copy.yaml"
        errors = validate_content(
            bundle(scenarios={SCENARIO_SOURCE: scenario(), other: scenario()}),
            "tianluo/e2e",
        )
        assert located(errors, other, "name")

    def test_bundle_must_be_a_mapping(self):
        assert validate_content(["nope"], "tianluo/e2e")

    def test_scenarios_must_be_a_mapping(self):
        errors = validate_content(bundle(scenarios=[scenario()]), "tianluo/e2e")
        assert located(errors, "tianluo/e2e", "scenarios")

    def test_every_problem_is_reported_in_one_pass(self):
        env = environment()
        env["services"][0].pop("image")
        env["services"][1]["name"] = "DB"
        scenarios = {SCENARIO_SOURCE: scenario(driver="ghost", timeout=-1)}
        errors = validate_content(bundle(env, scenarios), "tianluo/e2e")
        # missing image + bad service name + unknown driver + bad timeout
        assert len(errors) == 4
        assert located(errors, ENV_SOURCE, "services[0].image")
        assert located(errors, ENV_SOURCE, "services[1].name")
        assert located(errors, SCENARIO_SOURCE, "driver")
        assert located(errors, SCENARIO_SOURCE, "timeout")

    def test_driver_check_does_not_cascade_when_no_service_resolved(self):
        """A totally broken environment reports itself, not one error per scenario.

        Otherwise a single typo in the topology buries its own diagnosis under
        one "unknown driver" line for every scenario in the suite.
        """
        errors = validate_content(
            bundle({"services": [{"name": "App", "image": "x"}]}), "tianluo/e2e"
        )
        assert located(errors, ENV_SOURCE, "services[0].name")
        assert not located(errors, SCENARIO_SOURCE, "driver")

    def test_no_third_party_validation_dependency(self):
        import ast

        tree = ast.parse(Path(config_schema.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                imported.add(node.module.split(".")[0])
        assert imported <= {"re", "pathlib", "typing", "__future__", "tianluo"}


# --------------------------------------------------------------------------
# content_config loader
# --------------------------------------------------------------------------


def write_content(root: Path, *, env=None, scenarios=None, baselines=None) -> Path:
    """Materialize a tianluo/e2e/ directory under ``root``."""
    e2e_dir = root / "tianluo" / "e2e"
    e2e_dir.mkdir(parents=True, exist_ok=True)
    if env is not None:
        (e2e_dir / ENVIRONMENT_FILENAME).write_text(
            yaml.safe_dump(env, allow_unicode=True), encoding="utf-8"
        )
    if scenarios:
        scen_dir = e2e_dir / "scenarios"
        scen_dir.mkdir(exist_ok=True)
        for filename, data in scenarios.items():
            (scen_dir / filename).write_text(
                yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
            )
    for name, payload in (baselines or {}).items():
        base_dir = e2e_dir / "baselines"
        base_dir.mkdir(exist_ok=True)
        (base_dir / name).write_bytes(payload)
    return e2e_dir


class TestContentLoader:
    def test_absent_directory_is_a_sentinel_not_an_error(self, tmp_path):
        assert read_raw_content(tmp_path) is None
        assert load_content_config(tmp_path) is None

    def test_empty_directory_is_also_the_sentinel(self, tmp_path):
        (tmp_path / "tianluo" / "e2e").mkdir(parents=True)
        assert load_content_config(tmp_path) is None

    def test_scenarios_without_environment_is_an_error(self, tmp_path):
        write_content(tmp_path, scenarios={"smoke.yaml": scenario()})
        with pytest.raises(E2EConfigError) as excinfo:
            load_content_config(tmp_path)
        assert ENVIRONMENT_FILENAME in str(excinfo.value)

    def test_environment_without_scenarios_is_an_error(self, tmp_path):
        write_content(tmp_path, env=environment())
        with pytest.raises(E2EConfigError) as excinfo:
            load_content_config(tmp_path)
        assert "scenarios" in str(excinfo.value)

    def test_malformed_yaml_is_located(self, tmp_path):
        e2e_dir = write_content(
            tmp_path, env=environment(), scenarios={"smoke.yaml": scenario()}
        )
        (e2e_dir / ENVIRONMENT_FILENAME).write_text("services: [oops\n", encoding="utf-8")
        with pytest.raises(E2EConfigError) as excinfo:
            load_content_config(tmp_path)
        assert ENVIRONMENT_FILENAME in str(excinfo.value)

    def test_valid_directory_parses_into_declarations(self, tmp_path):
        write_content(
            tmp_path, env=environment(), scenarios={"smoke.yaml": scenario()}
        )
        content = load_content_config(tmp_path)
        assert content is not None
        assert [svc.name for svc in content.services] == ["app", "db"]
        assert [s.name for s in content.scenarios] == ["smoke"]
        assert content.scenario("smoke").driver == "app"
        assert content.service("db").mount_source is False

    def test_scenario_name_defaults_to_the_filename(self, tmp_path):
        data = scenario()
        del data["name"]
        write_content(tmp_path, env=environment(), scenarios={"login.yaml": data})
        content = load_content_config(tmp_path)
        assert [s.name for s in content.scenarios] == ["login"]

    def test_invalid_content_raises_with_every_problem_listed(self, tmp_path):
        write_content(
            tmp_path,
            env={"services": [{"name": "App"}]},
            scenarios={"smoke.yaml": scenario(timeout=-1)},
        )
        with pytest.raises(E2EConfigError) as excinfo:
            load_content_config(tmp_path)
        message = str(excinfo.value)
        assert "environment.yaml" in message
        assert "smoke.yaml" in message

    def test_baseline_existence_is_enforced_by_the_loader(self, tmp_path):
        assertion = {
            "kind": "screenshot_diff",
            "baseline": "home.png",
            "visual_regression": True,
        }
        write_content(
            tmp_path,
            env=environment(),
            scenarios={"smoke.yaml": scenario(assertions=[assertion])},
        )
        with pytest.raises(E2EConfigError):
            load_content_config(tmp_path)

        write_content(tmp_path, baselines={"home.png": b"\x89PNG"})
        assert load_content_config(tmp_path) is not None

    def test_legacy_se3_layout_is_honoured(self, tmp_path):
        """Paths route through runtime_paths, so a legacy checkout still loads."""
        legacy = tmp_path / "se3" / "e2e"
        legacy.mkdir(parents=True)
        (legacy / ENVIRONMENT_FILENAME).write_text(
            yaml.safe_dump(environment()), encoding="utf-8"
        )
        (legacy / "scenarios").mkdir()
        (legacy / "scenarios" / "smoke.yaml").write_text(
            yaml.safe_dump(scenario()), encoding="utf-8"
        )
        content = load_content_config(tmp_path)
        assert content is not None
        assert content.root == legacy

    def test_content_dir_is_not_hardcoded(self):
        source = Path(content_config.__file__).read_text(encoding="utf-8")
        assert "runtime_dir" in source
        assert '"tianluo/' not in source


class TestConversionToBackendSpecs:
    def test_environment_spec_round_trip(self, tmp_path):
        write_content(
            tmp_path, env=environment(), scenarios={"smoke.yaml": scenario()}
        )
        content = load_content_config(tmp_path)
        spec = content.to_environment_spec()

        assert spec.project_root == tmp_path
        assert [s.name for s in spec.services] == ["app", "db"]

        app = spec.service("app")
        assert app.base_image == "python:3.12-slim"
        # base_kind + build steps -> a locally built image from the base template.
        assert app.template == "base"
        assert app.build_steps == ("pip install -e .",)
        assert app.readiness.kind == "http"
        assert [(m.source, m.target) for m in app.mounts] == [(tmp_path, "/workspace")]
        assert app.workdir == "/workspace"

        db = spec.service("db")
        # A stock public image with no build steps is pulled as-is: no template,
        # and no source mount for an external dependency.
        assert db.template is None
        assert db.mounts == ()
        assert db.readiness.kind == "tcp"
        assert db.readiness.port == 5432

    def test_network_override(self, tmp_path):
        write_content(
            tmp_path, env=environment(), scenarios={"smoke.yaml": scenario()}
        )
        content = load_content_config(tmp_path)
        assert content.to_environment_spec(network="flow-42").network == "flow-42"

    def test_declared_network_is_used_by_default(self, tmp_path):
        write_content(
            tmp_path,
            env=environment(network="proj-net"),
            scenarios={"smoke.yaml": scenario()},
        )
        content = load_content_config(tmp_path)
        assert content.to_environment_spec().network == "proj-net"

    def test_gui_service_selects_its_template(self, tmp_path):
        env = {
            "services": [
                {"name": "gui", "image": "debian:stable-slim", "base_kind": "gui-xvfb"}
            ]
        }
        write_content(
            tmp_path, env=env, scenarios={"smoke.yaml": scenario(driver="gui")}
        )
        content = load_content_config(tmp_path)
        assert content.to_environment_spec().service("gui").template == "gui-xvfb"
