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
                "ports": ["18000:8000"],
                "environment": {"APP_ENV": "test"},
                # `http`/`tcp` probes are dialled from the host, so they name the
                # published host port — see TestReadinessReachability.
                "readiness": {
                    "kind": "http",
                    "url": "http://127.0.0.1:18000/healthz",
                },
            },
            {
                "name": "db",
                "image": "postgres:16",
                "mount_source": False,
                "ports": ["15432:5432"],
                "readiness": {"kind": "tcp", "port": 15432},
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
            {"kind": "http", "url": "http://127.0.0.1:18000/"},
            {"kind": "tcp", "port": 18000},
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
            "kind": "http", "url": "http://127.0.0.1:18000/", budget: 0
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


class TestReadinessReachability:
    """`http`/`tcp` probes are dialled from the host, so they must be dialable.

    Both rejected shapes read as perfectly sensible YAML and fail identically at
    run time: the probe polls an address that can never answer, spends its whole
    budget, and reports a healthy service as an environment failure.
    """

    def probe_errors(self, probe, *, ports=None):
        env = environment()
        env["services"][0]["readiness"] = probe
        if ports is not None:
            env["services"][0]["ports"] = ports
        errors, _ = validate_environment(env, ENV_SOURCE)
        return located(errors, ENV_SOURCE, "services[0].readiness")

    def test_http_probe_naming_an_in_network_service_is_rejected(self):
        """`app:8000` resolves only inside the container network."""
        assert self.probe_errors({"kind": "http", "url": "http://app:8000/health"})

    def test_http_probe_on_an_unpublished_loopback_port_is_rejected(self):
        assert self.probe_errors(
            {"kind": "http", "url": "http://localhost:8000/health"},
            ports=["18000:8000"],
        )

    def test_http_probe_on_the_published_host_port_is_accepted(self):
        assert self.probe_errors(
            {"kind": "http", "url": "http://127.0.0.1:18000/health"},
            ports=["18000:8000"],
        ) == []

    def test_a_service_publishing_nothing_cannot_carry_an_http_probe(self):
        assert self.probe_errors(
            {"kind": "http", "url": "http://localhost:8000/health"}, ports=[]
        )

    def test_command_probes_are_unconstrained_because_they_run_inside(self):
        assert self.probe_errors(
            {"kind": "command", "command": ["curl", "-fsS", "http://app:8000/health"]},
            ports=[],
        ) == []

    def test_tcp_probe_defaults_to_loopback_and_needs_the_port_published(self):
        assert self.probe_errors({"kind": "tcp", "port": 5432}, ports=["15432:5432"])
        assert self.probe_errors({"kind": "tcp", "port": 15432}, ports=["15432:5432"]) == []

    def test_an_ephemeral_host_port_mapping_is_not_second_guessed(self):
        """`-p 8000` picks the host port at random; nothing can be asserted."""
        assert self.probe_errors(
            {"kind": "http", "url": "http://127.0.0.1:8000/"}, ports=["8000"]
        ) == []

    def test_an_external_host_is_left_alone(self):
        assert self.probe_errors(
            {"kind": "http", "url": "http://health.example.test/live"}, ports=[]
        ) == []

    def test_expected_http_status_is_accepted(self):
        """A health route that answers 401 when up must still be probeable."""
        assert self.probe_errors(
            {"kind": "http", "url": "http://127.0.0.1:18000/", "status": 401},
            ports=["18000:8000"],
        ) == []

    @pytest.mark.parametrize("status", [99, 600, "200", True])
    def test_a_nonsensical_expected_status_is_rejected(self, status):
        env = environment()
        env["services"][0]["readiness"] = {
            "kind": "http", "url": "http://127.0.0.1:18000/", "status": status,
        }
        errors, _ = validate_environment(env, ENV_SOURCE)
        assert located(errors, ENV_SOURCE, "services[0].readiness.status")


class TestReadinessFieldTypes:
    """Presence is not enough: the conversion layer coerces these values.

    ``ServiceDecl.to_spec`` runs ``int(port)`` and ``tuple(str(part) for ...)``
    on whatever survived validation, so a malformed field that only had its
    *name* checked would surface as a bare ValueError traceback out of the E2E
    step instead of a located configuration error.
    """

    def field_errors(self, probe, path):
        env = environment()
        env["services"][0]["readiness"] = probe
        env["services"][0]["ports"] = ["18000:8000", "15432:5432"]
        errors, _ = validate_environment(env, ENV_SOURCE)
        # Prefix match rather than `located`: a bad *item* of an argv list is
        # reported at `...command[1]`, which is still the command field.
        prefix = f"{ENV_SOURCE}: {path}"
        return [message for message in errors if message.startswith(prefix)]

    @pytest.mark.parametrize("port", ["abc", "15432", 0, 70000, True, 1.5])
    def test_a_non_port_tcp_port_is_rejected(self, port):
        assert self.field_errors(
            {"kind": "tcp", "port": port}, "services[0].readiness.port"
        )

    @pytest.mark.parametrize("command", [5, {"run": "x"}, [], ["ok", 7]])
    def test_a_malformed_command_is_rejected(self, command):
        assert self.field_errors(
            {"kind": "command", "command": command}, "services[0].readiness.command"
        )

    def test_a_well_formed_command_still_validates(self):
        assert self.field_errors(
            {"kind": "command", "command": ["pg_isready"]},
            "services[0].readiness.command",
        ) == []

    @pytest.mark.parametrize("value", [5, ["http://x/"]])
    def test_a_non_string_url_is_rejected(self, value):
        assert self.field_errors(
            {"kind": "http", "url": value}, "services[0].readiness.url"
        )

    @pytest.mark.parametrize("budget", ["timeout", "interval"])
    @pytest.mark.parametrize("value", [float("inf"), float("nan")])
    def test_a_non_finite_readiness_budget_is_rejected(self, budget, value):
        """A NaN budget yields a deadline every comparison answers False to."""
        probe = {"kind": "tcp", "port": 15432, budget: value}
        assert self.field_errors(probe, f"services[0].readiness.{budget}")

    def test_a_non_finite_scenario_timeout_never_reaches_the_loader(self, tmp_path):
        """`timeout: .nan` must fail as located config, not as ValueError."""
        write_content(
            tmp_path,
            env=environment(),
            scenarios={"smoke.yaml": scenario(timeout=float("nan"))},
        )

        with pytest.raises(E2EConfigError):
            load_content_config(tmp_path)

    def test_a_malformed_probe_never_reaches_the_conversion_layer(self, tmp_path):
        """The loader must raise E2EConfigError, not ValueError from int()."""
        env = environment()
        env["services"][0]["readiness"] = {"kind": "tcp", "port": "abc"}
        write_content(tmp_path, env=env, scenarios={"smoke.yaml": scenario()})

        with pytest.raises(E2EConfigError):
            load_content_config(tmp_path)


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

    @pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
    def test_non_finite_timeout_is_rejected(self, value):
        """`.inf`/`.nan` survive a bare `<= 0` test and then crash the loader.

        YAML admits both spellings, and the conversion layer runs
        ``int(math.ceil(...))`` on whatever validation let through — inf raises
        OverflowError, NaN raises ValueError, neither of which locates the
        offending document.
        """
        assert located(
            self.validate(scenario(timeout=value)), SCENARIO_SOURCE, "timeout"
        )

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

    @pytest.mark.parametrize("value", ["true", 1, "no"])
    def test_a_non_boolean_fail_fast_is_rejected(self, value):
        """The parse layer reads flags by identity, so a string would be dropped.

        Validating it here is what keeps the parsed scenario in agreement with
        the document that was validated — the author's declaration is either
        honoured or reported, never silently discarded.
        """
        assert located(
            self.validate(scenario(fail_fast=value)), SCENARIO_SOURCE, "fail_fast"
        )

    def test_a_boolean_fail_fast_is_accepted(self):
        assert self.validate(scenario(fail_fast=True)) == []

    def test_a_dom_assertion_needs_a_playwright_driver(self):
        """Statically knowable, so it must not cost an image build to discover.

        The executor raises on this too, but only after every image is built and
        every readiness probe awaited — and the raise discards the results of the
        scenarios that already ran in the same round.
        """
        errors = self.validate(
            scenario(assertions=[{"kind": "dom", "selector": "h1"}]),
            service_kinds={"app": "base", "db": "base"},
        )
        assert located(errors, SCENARIO_SOURCE, "assertions[0]")

    def test_a_browser_action_needs_a_playwright_driver(self):
        errors = self.validate(
            scenario(
                actions=[{"action": "browser", "op": "goto", "url": "http://app:8000/"}]
            ),
            service_kinds={"app": "base", "db": "base"},
        )
        assert located(errors, SCENARIO_SOURCE, "actions[0]")

    def test_a_playwright_driver_may_drive_the_browser(self):
        assert self.validate(
            scenario(
                driver="browser",
                actions=[{"action": "browser", "op": "goto", "url": "http://app:8000/"}],
                assertions=[{"kind": "dom", "selector": "h1"}],
            ),
            services=("app", "browser"),
            service_kinds={"app": "base", "browser": "playwright"},
        ) == []

    def test_capability_is_not_checked_without_the_base_kind_map(self):
        """A caller validating a scenario alone only gets the name check."""
        assert self.validate(
            scenario(assertions=[{"kind": "dom", "selector": "h1"}])
        ) == []

    def test_a_bundle_rejects_a_dom_assertion_on_a_base_driver(self):
        """The whole-bundle entry point is where the two documents meet."""
        scenarios = {
            SCENARIO_SOURCE: scenario(assertions=[{"kind": "dom", "selector": "h1"}])
        }
        errors = validate_content(bundle(scenarios=scenarios), "tianluo/e2e")
        assert located(errors, SCENARIO_SOURCE, "assertions[0]")

    def test_a_bundle_accepts_a_dom_assertion_on_a_playwright_driver(self):
        env = environment()
        env["services"].append(
            {"name": "browser", "image": "mcr.microsoft.com/playwright:v1.47.0",
             "base_kind": "playwright"}
        )
        scenarios = {
            SCENARIO_SOURCE: scenario(
                driver="browser", assertions=[{"kind": "dom", "selector": "h1"}]
            )
        }
        assert validate_content(bundle(env, scenarios), "tianluo/e2e") == []

    def test_a_non_browser_action_after_a_browser_one_is_rejected(self):
        """Browser ops are batched last, so the declared order cannot hold."""
        errors = self.validate(
            scenario(
                actions=[
                    {"action": "browser", "op": "goto", "url": "http://app:8000/"},
                    {"action": "exec", "command": ["seed"]},
                ]
            )
        )
        assert located(errors, SCENARIO_SOURCE, "actions[1]")

    def test_non_browser_actions_declared_first_are_fine(self):
        assert self.validate(
            scenario(
                actions=[
                    {"action": "exec", "command": ["seed"]},
                    {"action": "browser", "op": "goto", "url": "http://app:8000/"},
                    {"action": "browser", "op": "click", "selector": "#go"},
                ]
            )
        ) == []


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

    # -- a flag is a declaration, not a truthy value -----------------------

    @pytest.mark.parametrize("value", ["false", "no", 1, [], "true"])
    def test_a_non_boolean_tier_flag_never_unlocks_a_tier(self, value):
        """`visual_regression: "false"` must not read as a declared escalation.

        Python truthiness would accept every non-empty string here — including
        one whose author plainly meant the opposite — so the flag that is meant
        to record a deliberate escalation would be satisfied by an accident.
        """
        errors = self.validate(
            {
                "kind": "screenshot_diff",
                "baseline": "home.png",
                "visual_regression": value,
            }
        )
        assert errors  # either a type error, or "tier 2 undeclared", or both
        assert not any("threshold" in error for error in errors)

    def test_a_non_boolean_evidence_flag_does_not_satisfy_the_evidence_rule(self):
        errors = self.validate(
            {
                "kind": "visual_semantic",
                "question": "Is the chart readable?",
                "semantic_visual": True,
                "require_evidence": "no",
            }
        )
        assert located(errors, SCENARIO_SOURCE, "assertions[0].require_evidence")
        assert located(errors, SCENARIO_SOURCE, "assertions[0]")

    # -- baseline shape ----------------------------------------------------

    def test_a_non_string_baseline_is_rejected(self, tmp_path):
        """Otherwise the baseline-existence rule is bypassable by any non-str."""
        errors = validate_scenario(
            scenario(
                assertions=[
                    {
                        "kind": "screenshot_diff",
                        "baseline": 123,
                        "visual_regression": True,
                    }
                ]
            ),
            SCENARIO_SOURCE,
            service_names=["app"],
            environment_source=ENV_SOURCE,
            baselines_dir=tmp_path / "baselines",
        )
        assert located(errors, SCENARIO_SOURCE, "assertions[0].baseline")

    def test_an_empty_baseline_is_rejected(self):
        errors = self.validate(
            {"kind": "screenshot_diff", "baseline": "  ", "visual_regression": True}
        )
        assert located(errors, SCENARIO_SOURCE, "assertions[0].baseline")

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

    @pytest.mark.parametrize(
        "baseline",
        [
            "/etc/hosts",
            "../../../etc/hosts",
            "sub/../../out.png",
            "C:/Windows/out.png",
            # Rooted but driveless: PureWindowsPath calls this *relative* (a
            # Windows path is absolute only with drive *and* root), yet joining it
            # keeps the drive and throws the baselines directory away — the shot
            # lands at the drive root. Drive-relative "C:out.png" re-anchors on
            # that drive's own cwd for the same reason.
            "\\out.png",
            "C:out.png",
        ],
    )
    def test_a_baseline_outside_the_directory_is_rejected(self, tmp_path, baseline):
        """Joining an anchored name onto the baselines dir discards the dir.

        Without containment a scenario could compare against — and
        ``--write-baselines`` could write — a file outside the git-tracked
        baselines directory, silently voiding the "baseline asset in git"
        contract. The check runs even when the file happens to exist.
        """
        errors = validate_scenario(
            scenario(
                assertions=[
                    {
                        "kind": "screenshot_diff",
                        "baseline": baseline,
                        "visual_regression": True,
                    }
                ]
            ),
            SCENARIO_SOURCE,
            service_names=["app"],
            environment_source=ENV_SOURCE,
            baselines_dir=tmp_path,
        )
        assert located(errors, SCENARIO_SOURCE, "assertions[0].baseline")

    def test_a_baseline_in_a_subdirectory_is_still_allowed(self, tmp_path):
        (tmp_path / "web").mkdir()
        (tmp_path / "web" / "home.png").write_bytes(b"x")
        errors = validate_scenario(
            scenario(
                assertions=[
                    {
                        "kind": "screenshot_diff",
                        "baseline": "web/home.png",
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

    def test_first_capture_admits_a_baseline_that_is_not_there_yet(self, tmp_path):
        """A baseline can only be produced by running the scenario once.

        So the caller that has asked for that first capture must be able to get
        past validation, while every *other* rule still applies.
        """
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
            require_existing_baselines=False,
        )
        assert errors == []

    def test_first_capture_does_not_relax_the_ladder(self, tmp_path):
        errors = validate_scenario(
            scenario(
                assertions=[{"kind": "screenshot_diff", "baseline": "home.png"}]
            ),
            SCENARIO_SOURCE,
            service_names=["app"],
            environment_source=ENV_SOURCE,
            baselines_dir=tmp_path,
            require_existing_baselines=False,
        )
        assert any("visual_regression" in message for message in errors)

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
        assert imported <= {
            "math", "re", "pathlib", "typing", "urllib", "__future__", "tianluo",
        }


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

    def test_only_the_half_present_shapes_are_marked_incomplete(self, tmp_path):
        """Bootstrap completes a half-present directory and only that.

        The distinction has to live in the exception type: a corrupted scenario
        file must not be mistaken for "an interrupted bootstrap" and quietly
        completed — it is content a person has to fix.
        """
        from tianluo.e2e.errors import E2EContentIncompleteError

        write_content(tmp_path, env=environment())
        with pytest.raises(E2EContentIncompleteError):
            read_raw_content(tmp_path)

        e2e_dir = write_content(
            tmp_path, env=environment(), scenarios={"smoke.yaml": scenario()}
        )
        (e2e_dir / "scenarios" / "smoke.yaml").write_text(
            "name: [unclosed\n", encoding="utf-8"
        )
        with pytest.raises(E2EConfigError) as excinfo:
            read_raw_content(tmp_path)
        assert not isinstance(excinfo.value, E2EContentIncompleteError)

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

    def test_a_fractional_timeout_survives_parsing(self, tmp_path):
        """Validation accepts any positive number; parsing must not lose it.

        Truncating 0.5s to 0 makes the executor read "no budget declared" and
        substitute the 300s config default — running a scenario the document
        said to cut off in half a second.
        """
        write_content(
            tmp_path,
            env=environment(),
            scenarios={"smoke.yaml": scenario(timeout=0.5)},
        )
        content = load_content_config(tmp_path)
        assert content.scenario("smoke").timeout == 1

    def test_declaration_flags_are_parsed_as_the_schema_validates_them(self, tmp_path):
        """A parsed scenario must not claim a tier the document never declared."""
        from tianluo.e2e.content_config import _build_scenario

        parsed = _build_scenario(
            {
                "name": "smoke",
                "driver": "app",
                "assertions": [
                    {
                        "kind": "screenshot_diff",
                        "baseline": "home.png",
                        "visual_regression": "false",
                    }
                ],
            },
            SCENARIO_SOURCE,
        )
        assert parsed.assertions[0].visual_regression is False

    def test_readiness_status_reaches_the_probe(self, tmp_path):
        env = environment()
        env["services"][0]["readiness"]["status"] = 401
        write_content(tmp_path, env=env, scenarios={"smoke.yaml": scenario()})
        content = load_content_config(tmp_path)
        assert content.to_environment_spec().service("app").readiness.status == 401

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

        # The escape hatch the first-capture path uses: same content, admitted.
        assert load_content_config(tmp_path, require_baselines=False) is not None

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
        assert db.readiness.port == 15432

    def test_a_string_readiness_command_is_wrapped_in_a_shell(self, tmp_path):
        """The schema admits a shell string; `backend.exec` runs argv directly.

        Handing ``("pg_isready -U app",)`` to exec would look for a binary whose
        name contains a space, fail every attempt, spend the probe's whole budget
        and report a healthy service as an environment failure — so the string
        must arrive shelled, exactly as the executor shells an action command.
        """
        env = environment(
            services=[
                {
                    "name": "db",
                    "image": "postgres:16",
                    "mount_source": False,
                    "readiness": {"kind": "command", "command": "pg_isready -U app"},
                }
            ]
        )
        write_content(
            tmp_path, env=env, scenarios={"smoke.yaml": scenario(driver="db")}
        )

        content = load_content_config(tmp_path)
        probe = content.to_environment_spec().service("db").readiness

        assert probe.command == ("sh", "-lc", "pg_isready -U app")

    def test_a_list_readiness_command_is_left_as_argv(self, tmp_path):
        """A declared argv list must not gain a shell it did not ask for."""
        env = environment(
            services=[
                {
                    "name": "db",
                    "image": "postgres:16",
                    "mount_source": False,
                    "readiness": {
                        "kind": "command",
                        "command": ["pg_isready", "-U", "app"],
                    },
                }
            ]
        )
        write_content(
            tmp_path, env=env, scenarios={"smoke.yaml": scenario(driver="db")}
        )

        content = load_content_config(tmp_path)
        probe = content.to_environment_spec().service("db").readiness

        assert probe.command == ("pg_isready", "-U", "app")

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
