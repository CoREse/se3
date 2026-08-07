"""Tests for the session orchestrator (:func:`tianluo.e2e.session.run_e2e`).

The two properties worth the most here are the *ordering* guarantees, because
both are invisible in normal operation and expensive when broken: nothing may be
created before preflight passes, and teardown must run even when a scenario
explodes. Both are asserted against the shared :class:`FakeBackend`, which
counts every create/start/destroy.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from tianluo.config import E2EConfig
from tianluo.e2e.backend import ExecResult
from tianluo.e2e.errors import (
    E2EConfigError,
    E2EEnvironmentError,
    E2EScenarioFailure,
)
from tianluo.e2e.session import FIX_REASON, E2EVerdict, run_e2e

from ._stubs import FakeBackend, assertion, content, scenario, service_decl


def working_runner(argv, **kwargs):
    """A ``docker info`` that succeeds, so preflight passes."""
    return subprocess.CompletedProcess(argv, 0, "{}", "")


def broken_runner(argv, **kwargs):
    """Every runtime probe fails — the "no usable container runtime" host."""
    return subprocess.CompletedProcess(argv, 1, "", "permission denied")


def passing_scenario(name="smoke"):
    return scenario(
        name,
        driver="app",
        actions=(),
        assertions=(assertion("file_exists", path="/workspace"),),
        source="tianluo/e2e/scenarios/{}.yaml".format(name),
    )


def failing_scenario(name="broken"):
    return scenario(
        name,
        driver="app",
        assertions=(assertion("file_exists", path="/nope"),),
        source="tianluo/e2e/scenarios/{}.yaml".format(name),
    )


def enabled_config(**kwargs):
    return E2EConfig(enabled=True, **kwargs)


class RecordingFactory:
    """Backend factory that records whether it was ever asked for a backend."""

    def __init__(self, backend: FakeBackend) -> None:
        self.backend = backend
        self.calls = []

    def __call__(self, probe, **kwargs):
        self.calls.append((probe, kwargs))
        return self.backend


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


class TestPreflight:
    def test_failure_short_circuits_before_anything_is_created(self, tmp_path):
        backend = FakeBackend()
        factory = RecordingFactory(backend)

        verdict = run_e2e(
            tmp_path,
            config=enabled_config(),
            content=content(tmp_path, scenarios=(passing_scenario(),)),
            backend_factory=factory,
            runner=broken_runner,
        )

        assert verdict.passed is False
        assert verdict.environment_error
        assert verdict.remediation
        # INVARIANT under test: no network, no image, no container.
        assert factory.calls == []
        assert backend.created == []
        assert backend.started == []
        assert backend.destroyed == 0

    def test_environment_failure_never_enters_the_fix_loop(self, tmp_path):
        verdict = run_e2e(
            tmp_path,
            config=enabled_config(),
            content=content(tmp_path, scenarios=(passing_scenario(),)),
            backend_factory=RecordingFactory(FakeBackend()),
            runner=broken_runner,
        )

        assert verdict.should_fix is False
        assert verdict.fix_instructions == ""
        assert verdict.fix_context == {}
        assert verdict.as_failure() is None

    def test_remediation_mentions_how_to_get_a_runtime(self, tmp_path):
        verdict = run_e2e(
            tmp_path,
            config=enabled_config(),
            content=content(tmp_path, scenarios=(passing_scenario(),)),
            backend_factory=RecordingFactory(FakeBackend()),
            runner=broken_runner,
        )

        assert "docker" in verdict.remediation and "podman" in verdict.remediation

    def test_probe_result_reaches_the_backend_factory(self, tmp_path):
        backend = FakeBackend()
        factory = RecordingFactory(backend)

        run_e2e(
            tmp_path,
            config=enabled_config(oci_runtime="kata", build_timeout=99),
            content=content(tmp_path, scenarios=(passing_scenario(),)),
            backend_factory=factory,
            runner=working_runner,
        )

        probe, kwargs = factory.calls[0]
        assert probe.name == "docker" and probe.ok
        assert kwargs["oci_runtime"] == "kata"
        assert kwargs["build_timeout"] == 99.0

    def test_injected_backend_skips_probing_entirely(self, tmp_path):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "{}", "")

        run_e2e(
            tmp_path,
            config=enabled_config(),
            content=content(tmp_path, scenarios=(passing_scenario(),)),
            backend=FakeBackend(),
            runner=runner,
        )

        assert calls == []


# ---------------------------------------------------------------------------
# configuration and selection
# ---------------------------------------------------------------------------


class TestConfigurationAndSelection:
    def test_absent_content_directory_is_reported_with_its_path(self, tmp_path):
        backend = FakeBackend()

        with pytest.raises(E2EConfigError) as excinfo:
            run_e2e(tmp_path, config=enabled_config(), backend=backend)

        assert "e2e" in str(excinfo.value)
        assert backend.created == []

    def test_content_is_loaded_from_disk_when_not_injected(self, tmp_path):
        _write_content(tmp_path)
        backend = FakeBackend()

        verdict = run_e2e(tmp_path, config=enabled_config(), backend=backend)

        assert verdict.passed, verdict.summary
        assert verdict.summary["declared_scenarios"] == ["cli-smoke"]
        assert backend.created[0].services[0].name == "app"

    def test_configured_selection_limits_the_run(self, tmp_path):
        backend = FakeBackend()

        verdict = run_e2e(
            tmp_path,
            config=enabled_config(scenarios=["second"]),
            content=content(
                tmp_path,
                scenarios=(passing_scenario("first"), passing_scenario("second")),
            ),
            backend=backend,
        )

        assert verdict.summary["selected_scenarios"] == ["second"]
        assert [r.name for r in verdict.scenario_results] == ["second"]

    def test_explicit_argument_overrides_the_configured_selection(self, tmp_path):
        verdict = run_e2e(
            tmp_path,
            scenarios=["first"],
            config=enabled_config(scenarios=["second"]),
            content=content(
                tmp_path,
                scenarios=(passing_scenario("first"), passing_scenario("second")),
            ),
            backend=FakeBackend(),
        )

        assert [r.name for r in verdict.scenario_results] == ["first"]

    def test_unknown_selected_scenario_is_an_error_not_an_empty_run(self, tmp_path):
        backend = FakeBackend()

        with pytest.raises(E2EConfigError) as excinfo:
            run_e2e(
                tmp_path,
                scenarios=["typo"],
                config=enabled_config(),
                content=content(tmp_path, scenarios=(passing_scenario("smoke"),)),
                backend=backend,
            )

        assert "typo" in str(excinfo.value)
        assert "smoke" in str(excinfo.value)
        assert backend.created == []

    def test_unknown_configured_scenario_is_also_refused(self, tmp_path):
        with pytest.raises(E2EConfigError):
            run_e2e(
                tmp_path,
                config=enabled_config(scenarios=["ghost"]),
                content=content(tmp_path, scenarios=(passing_scenario(),)),
                backend=FakeBackend(),
            )

    def test_nothing_selected_builds_no_environment(self, tmp_path):
        backend = FakeBackend()

        verdict = run_e2e(
            tmp_path,
            config=enabled_config(),
            content=content(tmp_path, scenarios=()),
            backend=backend,
        )

        assert verdict.passed
        assert verdict.summary["total"] == 0
        assert backend.created == []

    def test_network_suffix_isolates_concurrent_runs(self, tmp_path):
        backend = FakeBackend()

        run_e2e(
            tmp_path,
            config=enabled_config(),
            content=content(tmp_path, scenarios=(passing_scenario(),)),
            backend=backend,
            network_suffix="Flow_20260807/A",
        )

        network = backend.created[0].network
        assert network.startswith("tianluo-e2e-")
        assert network == network.lower()
        assert "/" not in network and "_" not in network


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_happy_path_creates_starts_and_destroys(self, tmp_path):
        backend = FakeBackend()

        verdict = run_e2e(
            tmp_path,
            config=enabled_config(),
            content=content(tmp_path, scenarios=(passing_scenario(),)),
            backend=backend,
        )

        assert verdict.passed
        assert len(backend.created) == 1
        assert len(backend.started) == 1
        assert backend.destroyed == 1

    def test_destroy_still_runs_when_a_scenario_explodes(self, tmp_path):
        backend = FakeBackend(exec_error=RuntimeError("driver went away"))

        with pytest.raises(RuntimeError):
            run_e2e(
                tmp_path,
                config=enabled_config(),
                content=content(tmp_path, scenarios=(passing_scenario(),)),
                backend=backend,
            )

        assert backend.destroyed == 1

    def test_keep_environment_skips_teardown(self, tmp_path, caplog):
        backend = FakeBackend()

        with caplog.at_level("INFO"):
            run_e2e(
                tmp_path,
                config=enabled_config(keep_environment=True),
                content=content(tmp_path, scenarios=(passing_scenario(),)),
                backend=backend,
            )

        assert backend.destroyed == 0
        kept = "\n".join(caplog.messages)
        assert "keep_environment" in kept
        # The hint must say how to clean up by hand, or the leak is on us.
        assert "network rm" in kept

    def test_keep_environment_argument_overrides_the_config(self, tmp_path):
        backend = FakeBackend()

        run_e2e(
            tmp_path,
            config=enabled_config(keep_environment=True),
            content=content(tmp_path, scenarios=(passing_scenario(),)),
            backend=backend,
            keep_environment=False,
        )

        assert backend.destroyed == 1

    def test_readiness_is_awaited_for_every_declared_probe(self, tmp_path):
        backend = FakeBackend()
        services = (
            service_decl(
                "app",
                readiness={"kind": "command", "command": ["pg_isready"], "timeout": 5},
            ),
        )

        run_e2e(
            tmp_path,
            config=enabled_config(),
            content=content(
                tmp_path, services=services, scenarios=(passing_scenario(),)
            ),
            backend=backend,
        )

        assert backend.argv_containing("pg_isready")

    def test_readiness_timeout_is_an_environment_error(self, tmp_path):
        backend = FakeBackend(
            exec_handler=lambda service, argv: ExecResult(exit_code=1)
        )
        services = (
            service_decl(
                "app",
                readiness={
                    "kind": "command",
                    "command": ["pg_isready"],
                    "timeout": 0.01,
                    "interval": 0,
                },
            ),
        )

        verdict = run_e2e(
            tmp_path,
            config=enabled_config(),
            content=content(
                tmp_path, services=services, scenarios=(passing_scenario(),)
            ),
            backend=backend,
        )

        assert verdict.environment_error
        assert verdict.should_fix is False
        # Teardown still happened, so the next run starts clean.
        assert backend.destroyed == 1

    def test_start_failure_is_an_environment_error_with_remediation(self, tmp_path):
        backend = FakeBackend(
            start_error=E2EEnvironmentError("could not start", remediation="do X")
        )

        verdict = run_e2e(
            tmp_path,
            config=enabled_config(),
            content=content(tmp_path, scenarios=(passing_scenario(),)),
            backend=backend,
        )

        assert verdict.environment_error == "could not start"
        assert verdict.remediation == "do X"
        assert backend.destroyed == 1

    def test_create_failure_leaves_nothing_to_tear_down(self, tmp_path):
        backend = FakeBackend(create_error=E2EEnvironmentError("no disk space"))

        verdict = run_e2e(
            tmp_path,
            config=enabled_config(),
            content=content(tmp_path, scenarios=(passing_scenario(),)),
            backend=backend,
        )

        assert verdict.environment_error == "no disk space"
        assert backend.destroyed == 0


# ---------------------------------------------------------------------------
# verdict shape
# ---------------------------------------------------------------------------


class TestVerdict:
    def _failing_verdict(self, tmp_path, **kwargs):
        backend = FakeBackend(
            exec_handler=lambda service, argv: ExecResult(exit_code=1),
            log_text="line1\nTraceback: boom\n",
        )
        verdict = run_e2e(
            tmp_path,
            config=enabled_config(),
            content=content(tmp_path, scenarios=(failing_scenario(),)),
            backend=backend,
            **kwargs,
        )
        return backend, verdict

    def test_scenario_failure_routes_to_the_fix_loop(self, tmp_path):
        _, verdict = self._failing_verdict(tmp_path)

        assert verdict.passed is False
        assert verdict.environment_error == ""
        assert verdict.should_fix is True

    def test_fix_instructions_quote_expected_and_actual(self, tmp_path):
        _, verdict = self._failing_verdict(tmp_path)

        text = verdict.fix_instructions
        assert "broken" in text
        assert "expected:" in text and "actual:" in text
        assert "/nope" in text

    def test_fix_instructions_include_the_container_log_tail(self, tmp_path):
        backend, verdict = self._failing_verdict(tmp_path)

        assert "Traceback: boom" in verdict.fix_instructions
        # The log was captured while the container still existed.
        assert ("app", "", "log") in backend.snapshot_calls

    def test_fix_context_is_structured(self, tmp_path):
        _, verdict = self._failing_verdict(tmp_path)

        assert verdict.fix_context["reason"] == FIX_REASON
        assert verdict.fix_context["scenarios_failed"] == ["broken"]
        issue = verdict.fix_context["issues"][0]
        assert issue["scenario"] == "broken"
        assert issue["kind"] == "file_exists"
        assert issue["tier"] == 1
        assert issue["expected"] and issue["actual"]

    def test_summary_is_the_structured_step_output(self, tmp_path):
        backend = FakeBackend(
            exec_handler=lambda service, argv: ExecResult(
                exit_code=0 if "workspace" in argv[-1] else 1
            )
        )

        verdict = run_e2e(
            tmp_path,
            config=enabled_config(),
            content=content(
                tmp_path, scenarios=(passing_scenario("ok"), failing_scenario("bad"))
            ),
            backend=backend,
        )

        summary = verdict.summary
        assert summary["total"] == 2
        assert summary["passed"] == 1 and summary["failed"] == 1
        assert summary["scenarios_passed"] == ["ok"]
        assert summary["scenarios_failed"] == ["bad"]
        assert summary["runtime"] == "fake"
        assert [s["name"] for s in summary["scenarios"]] == ["ok", "bad"]

    def test_as_failure_exposes_the_exception_form(self, tmp_path):
        _, verdict = self._failing_verdict(tmp_path)

        failure = verdict.as_failure()

        assert isinstance(failure, E2EScenarioFailure)
        assert "broken" in str(failure)
        assert len(failure.results) == 1

    def test_passing_verdict_carries_no_fix_payload(self, tmp_path):
        verdict = run_e2e(
            tmp_path,
            config=enabled_config(),
            content=content(tmp_path, scenarios=(passing_scenario(),)),
            backend=FakeBackend(),
        )

        assert verdict.should_fix is False
        assert verdict.fix_instructions == "" and verdict.fix_context == {}
        assert verdict.failed_scenarios == []

    def test_verdict_mirrors_the_test_verdict_field_names(self):
        """The E2E handler is a thin mapping, so the two shapes must line up."""
        from dataclasses import fields

        from tianluo.engine.steps.test import TestVerdict

        e2e_fields = {field.name for field in fields(E2EVerdict)}
        test_fields = {field.name for field in fields(TestVerdict)}

        # The shared vocabulary between the two check steps.
        assert {"fix_instructions", "fix_context"} <= e2e_fields & test_fields
        assert "should_fix" in dir(E2EVerdict) and "should_fix" in test_fields
        # And the field mapping is documented rather than left to be guessed.
        assert "TestVerdict" in (E2EVerdict.__doc__ or "")


# ---------------------------------------------------------------------------
# on-disk content
# ---------------------------------------------------------------------------


def _write_content(project_root: Path) -> None:
    """Write a minimal valid ``tianluo/e2e/`` tree."""
    root = project_root / "tianluo" / "e2e"
    (root / "scenarios").mkdir(parents=True)
    (root / "environment.yaml").write_text(
        yaml.safe_dump(
            {
                "network": "tianluo-e2e",
                "services": [
                    {
                        "name": "app",
                        "image": "python:3.12-slim",
                        "base_kind": "base",
                        "build": ["pip install -e ."],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "scenarios" / "cli-smoke.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "cli-smoke",
                "driver": "app",
                "actions": [{"action": "exec", "command": ["luo", "--version"]}],
                "assertions": [{"kind": "exit_code", "equals": 0}],
            }
        ),
        encoding="utf-8",
    )


class TestOnDiskContent:
    def test_yaml_scenario_runs_end_to_end_against_the_fake_backend(self, tmp_path):
        _write_content(tmp_path)
        backend = FakeBackend()

        verdict = run_e2e(tmp_path, config=enabled_config(), backend=backend)

        assert verdict.passed
        assert backend.argv_containing("luo")
        assert verdict.summary["selected_scenarios"] == ["cli-smoke"]

    def test_invalid_yaml_content_propagates_as_a_config_error(self, tmp_path):
        root = tmp_path / "tianluo" / "e2e"
        (root / "scenarios").mkdir(parents=True)
        (root / "environment.yaml").write_text(
            yaml.safe_dump({"services": [{"name": "app", "image": "python"}]}),
            encoding="utf-8",
        )
        (root / "scenarios" / "bad.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "bad",
                    "driver": "app",
                    # Tier 2 without the opt-in: the ladder must refuse it.
                    "assertions": [{"kind": "screenshot_diff", "baseline": "x.png"}],
                }
            ),
            encoding="utf-8",
        )
        backend = FakeBackend()

        with pytest.raises(E2EConfigError) as excinfo:
            run_e2e(tmp_path, config=enabled_config(), backend=backend)

        assert "visual_regression" in str(excinfo.value)
        assert backend.created == []

    def test_config_is_loaded_from_the_project_when_not_passed(self, tmp_path):
        _write_content(tmp_path)
        (tmp_path / "tianluo.yaml").write_text(
            yaml.safe_dump({"e2e": {"enabled": True, "scenarios": ["cli-smoke"]}}),
            encoding="utf-8",
        )
        backend = FakeBackend()

        verdict = run_e2e(tmp_path, backend=backend)

        assert verdict.passed
        assert verdict.summary["selected_scenarios"] == ["cli-smoke"]
