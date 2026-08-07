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

from ._stubs import (
    FakeBackend,
    FakeClock,
    action,
    assertion,
    content,
    scenario,
    service_decl,
)


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

    def test_a_critical_scenario_runs_even_when_the_selection_excludes_it(
        self, tmp_path
    ):
        """The documented guarantee: critical scenarios genuinely run.

        Narrowing `scenarios` to keep a fix loop fast must not be able to skip a
        scenario the user declared load-bearing.
        """
        verdict = run_e2e(
            tmp_path,
            config=enabled_config(scenarios=["smoke"], critical_scenarios=["login"]),
            content=content(
                tmp_path,
                scenarios=(passing_scenario("smoke"), passing_scenario("login")),
            ),
            backend=FakeBackend(),
        )

        assert verdict.summary["selected_scenarios"] == ["smoke", "login"]
        assert verdict.summary["critical_scenarios"] == ["login"]
        assert verdict.passed

    def test_a_critical_scenario_is_added_to_an_explicit_scenario_argument_too(
        self, tmp_path
    ):
        verdict = run_e2e(
            tmp_path,
            scenarios=["smoke"],
            config=enabled_config(critical_scenarios=["login"]),
            content=content(
                tmp_path,
                scenarios=(passing_scenario("smoke"), passing_scenario("login")),
            ),
            backend=FakeBackend(),
        )

        assert [r.name for r in verdict.scenario_results] == ["smoke", "login"]

    def test_a_selected_critical_scenario_is_not_run_twice(self, tmp_path):
        verdict = run_e2e(
            tmp_path,
            config=enabled_config(scenarios=["smoke"], critical_scenarios=["smoke"]),
            content=content(tmp_path, scenarios=(passing_scenario("smoke"),)),
            backend=FakeBackend(),
        )

        assert [r.name for r in verdict.scenario_results] == ["smoke"]

    def test_a_failing_critical_scenario_fails_the_run(self, tmp_path):
        verdict = run_e2e(
            tmp_path,
            config=enabled_config(critical_scenarios=["broken"]),
            content=content(
                tmp_path,
                scenarios=(passing_scenario("smoke"), failing_scenario("broken")),
            ),
            backend=FakeBackend(
                exec_handler=lambda service, argv: ExecResult(exit_code=1)
            ),
        )

        assert verdict.passed is False
        assert verdict.should_fix is True

    def test_an_undeclared_critical_scenario_is_a_configuration_error(self, tmp_path):
        backend = FakeBackend()

        with pytest.raises(E2EConfigError) as excinfo:
            run_e2e(
                tmp_path,
                config=enabled_config(critical_scenarios=["ghost"]),
                content=content(tmp_path, scenarios=(passing_scenario("smoke"),)),
                backend=backend,
            )

        assert "ghost" in str(excinfo.value)
        assert "smoke" in str(excinfo.value)
        # A promise that cannot be met stops the run before anything is built.
        assert backend.created == []

    def test_an_unverified_critical_scenario_cannot_pass(self, tmp_path):
        """Defensive net: no result for a critical scenario is not a pass."""
        from tianluo.e2e.session import _verdict_for

        bundle = content(tmp_path, scenarios=(passing_scenario("login"),))
        verdict = _verdict_for(bundle, [], [], "docker", ["login"])

        assert verdict.passed is False
        assert verdict.should_fix is True
        assert verdict.summary["critical_unverified"] == ["login"]
        assert "login" in verdict.fix_instructions
        assert verdict.fix_context["critical_unverified"] == ["login"]
        assert verdict.fix_context["issues"][0]["kind"] == (
            "critical_scenario_not_verified"
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

        # No at_level(): a real `luo` run installs no logging handler, so
        # anything the session emits below WARNING never reaches the user.
        verdict = run_e2e(
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
        assert [record.levelname for record in caplog.records] == ["WARNING"]

        # And it must be handed to the caller as data, so the CLI can print it
        # without depending on logging configuration at all.
        assert len(verdict.notices) == 1
        assert "network rm" in verdict.notices[0]
        kept_summary = verdict.summary["kept_environment"]
        assert kept_summary["network"] == "tianluo-e2e"
        assert kept_summary["containers"] == ["app"]

    def test_a_torn_down_environment_produces_no_cleanup_notice(self, tmp_path):
        verdict = run_e2e(
            tmp_path,
            config=enabled_config(),
            content=content(tmp_path, scenarios=(passing_scenario(),)),
            backend=FakeBackend(),
        )

        assert verdict.notices == []
        assert "kept_environment" not in verdict.summary

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

    def test_a_partially_created_environment_is_still_torn_down(self, tmp_path):
        """`create` raising must not orphan the network it already made.

        The real backend creates the shared network first and *then* builds each
        service image, so a failing build leaves a live network behind while the
        exception discards the handle. Teardown therefore has to fall back to the
        handle the backend published before it started.
        """
        backend = FakeBackend(create_error=E2EEnvironmentError("no disk space"))

        verdict = run_e2e(
            tmp_path,
            config=enabled_config(),
            content=content(tmp_path, scenarios=(passing_scenario(),)),
            backend=backend,
        )

        assert verdict.environment_error == "no disk space"
        assert backend.destroyed == 1

    def test_nothing_is_torn_down_when_create_was_never_reached(self, tmp_path):
        backend = FakeBackend()

        run_e2e(
            tmp_path,
            config=enabled_config(),
            content=content(tmp_path, scenarios=(passing_scenario(),)),
            backend_factory=RecordingFactory(backend),
            runner=broken_runner,
        )

        assert backend.last_handle is None
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

    def test_the_log_tail_covers_every_service_not_only_the_driver(self, tmp_path):
        """The server-side traceback lives in the application service's log.

        In the primary web topology the driver is the Playwright container, so a
        driver-only capture hands the fix loop the browser's view of a failure
        whose cause is in another container's log.
        """

        class PerServiceLogs(FakeBackend):
            def snapshot(self, handle, service, target, *, kind="file",
                         destination=None):
                self.log_text = "{}: log line".format(service)
                return super().snapshot(
                    handle, service, target, kind=kind, destination=destination
                )

        backend = PerServiceLogs(
            exec_handler=lambda service, argv: ExecResult(exit_code=1)
        )
        verdict = run_e2e(
            tmp_path,
            config=enabled_config(),
            content=content(
                tmp_path,
                services=(service_decl("app"), service_decl("db", mount_source=False)),
                scenarios=(failing_scenario(),),
            ),
            backend=backend,
        )

        text = verdict.fix_instructions
        assert "app: log line" in text
        assert "db: log line" in text

    def test_action_trace_reaches_the_fix_instructions(self, tmp_path):
        """The failed command is usually the *cause* of the failed assertion.

        Without it in the prompt, the fix iteration debugs the symptom while the
        root cause stays invisible.
        """
        backend = FakeBackend(exec_handler=lambda service, argv: ExecResult(exit_code=3))
        verdict = run_e2e(
            tmp_path,
            config=enabled_config(),
            content=content(
                tmp_path,
                scenarios=(
                    scenario(
                        "broken",
                        driver="app",
                        actions=(action("exec", command=["run-me"]),),
                        assertions=(assertion("file_exists", path="/nope"),),
                    ),
                ),
            ),
            backend=backend,
        )

        assert "run-me" in verdict.fix_instructions
        assert "exited 3" in verdict.fix_instructions

    def test_an_action_failure_becomes_its_own_fix_issue(self, tmp_path):
        """An unreachable driving request is a failure no assertion adjudicates."""
        backend = FakeBackend()
        verdict = run_e2e(
            tmp_path,
            config=enabled_config(),
            content=content(
                tmp_path,
                scenarios=(
                    scenario(
                        "unreachable",
                        driver="app",
                        actions=(action("http", url="http://app:8000/x"),),
                        assertions=(assertion("file_exists", path="/present"),),
                    ),
                ),
            ),
            backend=backend,
        )

        assert verdict.should_fix is True
        kinds = [issue["kind"] for issue in verdict.fix_context["issues"]]
        assert "action" in kinds

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

    def test_summary_times_the_environment_apart_from_the_scenarios(self, tmp_path):
        """Two halves of the wall clock, two different diagnoses.

        Image builds and readiness waits dominate a cold run; folding them into
        the scenario total would leave "the suite is heavy" and "the image
        rebuilt" indistinguishable in the step output.
        """
        clock = FakeClock()

        class SlowStart(FakeBackend):
            def start(self, handle):
                clock.sleep(30)
                super().start(handle)

        verdict = run_e2e(
            tmp_path,
            config=enabled_config(),
            content=content(tmp_path, scenarios=(passing_scenario(),)),
            backend=SlowStart(),
            clock=clock,
            sleeper=clock.sleep,
        )

        assert verdict.summary["environment_duration"] >= 30
        assert verdict.summary["duration"] < verdict.summary["environment_duration"]

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


def _write_visual_content(project_root: Path) -> None:
    """Write content whose only assertion is a tier-2 diff with no baseline yet."""
    _write_content(project_root)
    root = project_root / "tianluo" / "e2e"
    (root / "scenarios" / "cli-smoke.yaml").unlink()
    (root / "scenarios" / "home.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "home",
                "driver": "app",
                "assertions": [
                    {
                        "kind": "screenshot_diff",
                        "baseline": "home.png",
                        "visual_regression": True,
                    }
                ],
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

    def test_an_absent_baseline_aborts_the_load_by_default(self, tmp_path):
        """Strict by default: an ordinary run cannot compare against nothing."""
        _write_visual_content(tmp_path)

        with pytest.raises(E2EConfigError) as excinfo:
            run_e2e(tmp_path, config=enabled_config(), backend=FakeBackend())

        assert "home.png" in str(excinfo.value)
        # The message has to say how to produce that first image.
        assert "--write-baselines" in str(excinfo.value)

    def test_write_missing_baselines_reaches_the_assertion_layer(self, tmp_path):
        """The regression this guards: first capture must be reachable.

        Content loading happens before any assertion runs, so a strict
        baseline-existence check there made ``--write-baselines`` — and every
        flow-authored tier-2 scenario — impossible to ever satisfy.
        """
        from ._stubs import png_bytes

        _write_visual_content(tmp_path)
        backend = FakeBackend(screenshot_bytes=png_bytes())

        verdict = run_e2e(
            tmp_path,
            config=enabled_config(),
            backend=backend,
            artifacts_dir=tmp_path / "artifacts",
            write_missing_baselines=True,
        )

        captured = tmp_path / "tianluo" / "e2e" / "baselines" / "home.png"
        assert captured.is_file()
        # Captured but deliberately not green: nobody has reviewed the image yet.
        assert verdict.passed is False
        assert verdict.should_fix is True

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
