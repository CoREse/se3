"""Tests for the ``E2E`` step handler.

Every test here runs on a host that may have neither docker nor podman: the
handler's single door to the e2e subsystem is ``session.run_e2e``, so stubbing
that function covers all four routes the handler can take. What the tests pin is
the *routing*, because that is where an e2e failure and an unusable host must not
be confused:

* e2e disabled            -> COMPLETED, subsystem never touched
* every scenario passed   -> COMPLETED
* a scenario failed       -> REVISION_NEEDED + fix_needed/instructions/context
* the host cannot run e2e -> FAILED, and NO fix_needed (stays out of the fix loop)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from tianluo.e2e.errors import (
    E2EConfigError,
    E2EDependencyMissingError,
    E2EEnvironmentError,
)
from tianluo.e2e.executor import AssertionResult, ScenarioResult
from tianluo.e2e.session import E2EVerdict
from tianluo.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)
from tianluo.engine.steps.e2e import e2e_handler


def _write_config(project_root: Path, *, enabled: bool) -> None:
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "tianluo.yaml").write_text(
        "e2e:\n  enabled: {}\n".format("true" if enabled else "false"),
        encoding="utf-8",
    )


def _flow(project_root: Path) -> FlowInstance:
    flow = FlowInstance(
        flow_id="flow-e2e-1",
        task_description="Add a login form",
        task_type="feature",
        status=FlowStatus.RUNNING,
    )
    flow.state.context["project_root"] = str(project_root)
    return flow


def _step() -> Step:
    return Step(step_type=StepType.E2E, status=StepStatus.RUNNING)


def _passing_verdict() -> E2EVerdict:
    result = ScenarioResult(name="login", passed=True, driver="app", duration=1.5)
    return E2EVerdict(
        passed=True,
        scenario_results=[result],
        summary={
            "runtime": "docker",
            "total": 1,
            "passed": 1,
            "failed": 0,
            "scenarios_passed": ["login"],
            "scenarios_failed": [],
            "duration": 1.5,
            "scenarios": [result.to_dict()],
        },
    )


def _failing_verdict() -> E2EVerdict:
    failed = ScenarioResult(
        name="login",
        passed=False,
        driver="browser",
        duration=2.0,
        assertions=[
            AssertionResult(
                kind="dom",
                tier=1,
                passed=False,
                expected="#welcome visible",
                actual="#welcome absent",
            )
        ],
    )
    return E2EVerdict(
        passed=False,
        scenario_results=[failed],
        fix_instructions="1 e2e scenario(s) failed.",
        fix_context={"reason": "e2e_failure", "scenarios_failed": ["login"], "issues": []},
        summary={
            "runtime": "podman",
            "total": 1,
            "passed": 0,
            "failed": 1,
            "scenarios_passed": [],
            "scenarios_failed": ["login"],
            "duration": 2.0,
            "scenarios": [failed.to_dict()],
        },
    )


class TestDisabledSwitch:
    """e2e off: the handler must not reach the subsystem at all."""

    def test_disabled_completes_without_running(self, tmp_path):
        _write_config(tmp_path, enabled=False)
        step, flow = _step(), _flow(tmp_path)

        with patch("tianluo.e2e.session.run_e2e") as run:
            assert e2e_handler(step, flow) == StepStatus.COMPLETED

        run.assert_not_called()
        assert step.outputs["e2e_results"]["skipped"] is True
        assert step.outputs["scenarios_failed"] == []
        assert "fix_needed" not in step.outputs

    def test_missing_config_file_is_disabled(self, tmp_path):
        """No tianluo.yaml at all means e2e is off (the default)."""
        step, flow = _step(), _flow(tmp_path)

        with patch("tianluo.e2e.session.run_e2e") as run:
            assert e2e_handler(step, flow) == StepStatus.COMPLETED

        run.assert_not_called()


class TestScenariosPassed:
    def test_all_passed_completes(self, tmp_path):
        _write_config(tmp_path, enabled=True)
        step, flow = _step(), _flow(tmp_path)

        with patch(
            "tianluo.e2e.session.run_e2e", return_value=_passing_verdict()
        ) as run:
            assert e2e_handler(step, flow) == StepStatus.COMPLETED

        assert run.call_count == 1
        assert step.outputs["e2e_passed"] is True
        assert step.outputs["scenarios_passed"] == ["login"]
        assert step.outputs["scenarios_failed"] == []
        assert step.outputs["e2e_results"]["runtime"] == "docker"
        assert "fix_needed" not in step.outputs
        assert not step.error_message

    def test_run_is_scoped_to_the_flow(self, tmp_path):
        """The network suffix and artifacts dir must be per-flow.

        Two concurrent worktree flows of one project would otherwise share a
        container network (peer service names resolving to the wrong container)
        and overwrite each other's screenshots.
        """
        _write_config(tmp_path, enabled=True)
        step, flow = _step(), _flow(tmp_path)

        with patch(
            "tianluo.e2e.session.run_e2e", return_value=_passing_verdict()
        ) as run:
            e2e_handler(step, flow)

        kwargs = run.call_args.kwargs
        assert kwargs["network_suffix"] == "flow-e2e-1"
        assert "flow-e2e-1" in str(kwargs["artifacts_dir"])
        # Artefacts must NOT land in the committed content directory, next to the
        # baseline screenshots.
        assert "e2e" in str(kwargs["artifacts_dir"])
        assert Path(kwargs["artifacts_dir"]).parts[-2:] == ("e2e", "flow-e2e-1")
        assert kwargs["config"].enabled is True

    def test_empty_selection_reported_as_completed(self, tmp_path):
        """A run that selected nothing is a pass but says so in the summary."""
        _write_config(tmp_path, enabled=True)
        step, flow = _step(), _flow(tmp_path)
        verdict = E2EVerdict(
            passed=True,
            summary={"total": 0, "passed": 0, "failed": 0, "selected_scenarios": []},
        )

        with patch("tianluo.e2e.session.run_e2e", return_value=verdict):
            assert e2e_handler(step, flow) == StepStatus.COMPLETED

        assert step.outputs["e2e_results"]["total"] == 0


class TestScenarioFailure:
    def test_failure_requests_revision(self, tmp_path):
        _write_config(tmp_path, enabled=True)
        step, flow = _step(), _flow(tmp_path)

        with patch("tianluo.e2e.session.run_e2e", return_value=_failing_verdict()):
            assert e2e_handler(step, flow) == StepStatus.REVISION_NEEDED

        assert step.outputs["fix_needed"] is True
        assert step.outputs["fix_instructions"]
        assert step.outputs["fix_context"]["reason"] == "e2e_failure"
        assert step.outputs["scenarios_failed"] == ["login"]
        assert step.outputs["scenarios_passed"] == []
        assert "login" in step.error_message
        # Not an environment problem — nothing for the operator to repair.
        assert "environment_error" not in step.outputs

    def test_failed_scenario_detail_survives_into_outputs(self, tmp_path):
        _write_config(tmp_path, enabled=True)
        step, flow = _step(), _flow(tmp_path)

        with patch("tianluo.e2e.session.run_e2e", return_value=_failing_verdict()):
            e2e_handler(step, flow)

        scenarios = step.outputs["e2e_results"]["scenarios"]
        assertion = scenarios[0]["assertions"][0]
        assert assertion["expected"] == "#welcome visible"
        assert assertion["actual"] == "#welcome absent"


class TestEnvironmentFailure:
    """The fix-loop firewall: a host problem must never become a fix iteration."""

    def test_verdict_environment_error_fails_without_fix(self, tmp_path):
        _write_config(tmp_path, enabled=True)
        step, flow = _step(), _flow(tmp_path)
        verdict = E2EVerdict(
            passed=False,
            environment_error="no usable container runtime",
            remediation="add your user to the docker group",
            summary={"environment_error": "no usable container runtime"},
        )

        with patch("tianluo.e2e.session.run_e2e", return_value=verdict):
            assert e2e_handler(step, flow) == StepStatus.FAILED

        assert "fix_needed" not in step.outputs
        assert step.outputs["environment_error"] == "no usable container runtime"
        assert "docker group" in step.outputs["e2e_remediation"]
        assert "docker group" in step.error_message

    def test_raised_environment_error_fails_without_fix(self, tmp_path):
        """run_e2e funnels most host problems into a verdict, but not all."""
        _write_config(tmp_path, enabled=True)
        step, flow = _step(), _flow(tmp_path)
        error = E2EEnvironmentError("podman info failed", remediation="install podman")

        with patch("tianluo.e2e.session.run_e2e", side_effect=error):
            assert e2e_handler(step, flow) == StepStatus.FAILED

        assert "fix_needed" not in step.outputs
        assert step.outputs["environment_error"] == "podman info failed"
        assert "install podman" in step.error_message

    def test_missing_extra_is_an_environment_failure(self, tmp_path):
        """A missing optional extra is a host problem with an install hint."""
        _write_config(tmp_path, enabled=True)
        step, flow = _step(), _flow(tmp_path)
        error = E2EDependencyMissingError("Pillow", feature="screenshot diffing")

        with patch("tianluo.e2e.session.run_e2e", side_effect=error):
            assert e2e_handler(step, flow) == StepStatus.FAILED

        assert "fix_needed" not in step.outputs
        assert "tianluo[e2e]" in step.error_message

    def test_config_error_fails_without_fix(self, tmp_path):
        _write_config(tmp_path, enabled=True)
        step, flow = _step(), _flow(tmp_path)
        error = E2EConfigError("tianluo/e2e/scenarios/login.yaml: driver 'web' unknown")

        with patch("tianluo.e2e.session.run_e2e", side_effect=error):
            assert e2e_handler(step, flow) == StepStatus.FAILED

        assert "fix_needed" not in step.outputs
        assert "login.yaml" in step.error_message
        assert step.outputs["e2e_results"]["config_error"]


class TestBootstrapHook:
    def test_absent_bootstrap_module_does_not_block(self, tmp_path):
        """Content already in place needs no generation, so a missing bootstrap
        module must not stop the run."""
        _write_config(tmp_path, enabled=True)
        step, flow = _step(), _flow(tmp_path)

        with patch("tianluo.e2e.session.run_e2e", return_value=_passing_verdict()):
            assert e2e_handler(step, flow) == StepStatus.COMPLETED

    def test_bootstrap_is_invoked_when_available(self, tmp_path, monkeypatch):
        import sys
        import types

        import tianluo.e2e as e2e_pkg

        calls = []

        class _Result:
            created = True

        module = types.ModuleType("tianluo.e2e.bootstrap")

        def ensure_content(project_root, flow):
            calls.append((Path(project_root), flow))
            return _Result()

        module.ensure_content = ensure_content
        monkeypatch.setitem(sys.modules, "tianluo.e2e.bootstrap", module)
        monkeypatch.setattr(e2e_pkg, "bootstrap", module, raising=False)

        _write_config(tmp_path, enabled=True)
        step, flow = _step(), _flow(tmp_path)

        with patch("tianluo.e2e.session.run_e2e", return_value=_passing_verdict()):
            assert e2e_handler(step, flow) == StepStatus.COMPLETED

        assert calls and calls[0][0] == tmp_path
        assert step.outputs["e2e_results"]["bootstrap"]


class TestNoTopLevelSubsystemImport:
    """Core dependency isolation, guarded mechanically rather than by review."""

    def test_module_source_defers_every_e2e_import(self):
        source = Path(
            __import__("tianluo.engine.steps.e2e", fromlist=["x"]).__file__
        ).read_text(encoding="utf-8")
        for line in source.splitlines():
            if line.startswith(("from ", "import ")):
                assert "e2e." not in line and "runtime_paths" not in line, (
                    "e2e subsystem imported at module level: " + line
                )


@pytest.mark.parametrize("status", [StepStatus.COMPLETED, StepStatus.REVISION_NEEDED])
def test_handler_returns_a_step_status(tmp_path, status):
    """Sanity: the handler's contract is a StepStatus, never a bool or None."""
    _write_config(tmp_path, enabled=True)
    step, flow = _step(), _flow(tmp_path)
    verdict = (
        _passing_verdict() if status == StepStatus.COMPLETED else _failing_verdict()
    )

    with patch("tianluo.e2e.session.run_e2e", return_value=verdict):
        assert e2e_handler(step, flow) is status


def test_fix_context_issues_carry_a_description(tmp_path):
    """Later fix iterations re-render fix_history through the engine's shared
    issue extractor, which knows nothing about e2e's own issue keys — without a
    description each bullet would be empty."""
    _write_config(tmp_path, enabled=True)
    step, flow = _step(), _flow(tmp_path)
    verdict = _failing_verdict()
    verdict.fix_context["issues"] = [
        {
            "scenario": "login",
            "source": "scenarios/login.yaml",
            "driver": "browser",
            "kind": "dom",
            "tier": 1,
            "expected": "#welcome visible",
            "actual": "#welcome absent",
            "message": "",
        }
    ]

    with patch("tianluo.e2e.session.run_e2e", return_value=verdict):
        assert e2e_handler(step, flow) == StepStatus.REVISION_NEEDED

    issue = step.outputs["fix_context"]["issues"][0]
    assert "login" in issue["description"]
    assert "#welcome absent" in issue["description"]
    assert issue["location"] == "scenarios/login.yaml"

    from tianluo.engine.steps._fix_context import extract_issue_display_fields

    severity, description, location = extract_issue_display_fields(issue)
    assert description and location
