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


@pytest.fixture(autouse=True)
def no_content_generation(monkeypatch):
    """Keep the content-maintenance hook from reaching a real agent.

    ``tmp_path`` never has a ``tianluo/e2e/`` directory, so with e2e enabled the
    handler would legitimately try to *generate* one and then to evolve it — both
    of which mean constructing an LLMCaller and calling an agent. These tests are
    about the handler's routing, not about content generation (``tests/e2e/
    test_bootstrap.py`` owns that), so both halves are neutralised here. The
    tests in :class:`TestContentMaintenance` install their own stand-ins on top.
    """
    from tianluo.e2e import bootstrap

    monkeypatch.setattr(bootstrap, "ensure_content", lambda *a, **k: None)
    monkeypatch.setattr(bootstrap, "evolve_content", lambda *a, **k: None)


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

    def test_an_unverified_critical_scenario_says_so(self, tmp_path):
        """The revision reason must not read '0 e2e scenario(s) failed'.

        A critical scenario that produced no result forces ``passed=False`` with
        an empty failed list; composing the headline from that list alone made
        error_message contradict itself everywhere it is the only thing shown
        (WebUI, history).
        """
        _write_config(tmp_path, enabled=True)
        step, flow = _step(), _flow(tmp_path)
        verdict = E2EVerdict(
            passed=False,
            fix_instructions="critical scenario 'login' never ran.",
            fix_context={"reason": "e2e_failure", "issues": []},
            summary={
                "total": 0, "passed": 0, "failed": 0,
                "scenarios_passed": [], "scenarios_failed": [],
                "critical_unverified": ["login"],
            },
        )

        with patch("tianluo.e2e.session.run_e2e", return_value=verdict):
            assert e2e_handler(step, flow) == StepStatus.REVISION_NEEDED

        assert "0 " not in step.error_message
        assert "login" in step.error_message

    def test_both_causes_are_named_when_both_hold(self, tmp_path):
        _write_config(tmp_path, enabled=True)
        step, flow = _step(), _flow(tmp_path)
        verdict = _failing_verdict()
        verdict.summary["critical_unverified"] = ["checkout"]

        with patch("tianluo.e2e.session.run_e2e", return_value=verdict):
            e2e_handler(step, flow)

        assert "login" in step.error_message
        assert "checkout" in step.error_message

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


def _install_bootstrap(monkeypatch, **members):
    """Put a stand-in ``tianluo.e2e.bootstrap`` in front of the real one."""
    import sys
    import types

    import tianluo.e2e as e2e_pkg

    module = types.ModuleType("tianluo.e2e.bootstrap")
    for name, value in members.items():
        setattr(module, name, value)
    monkeypatch.setitem(sys.modules, "tianluo.e2e.bootstrap", module)
    monkeypatch.setattr(e2e_pkg, "bootstrap", module, raising=False)
    return module


class _EvolveResult:
    """Stand-in for ``BootstrapResult`` after a successful evolution."""

    def __init__(self, changed=True, note="evolved", errors=()):
        self.changed = changed
        self.note = note
        self.errors = tuple(errors)
        self.written = ("tianluo/e2e/scenarios/health.yaml",) if changed else ()
        self.created = False


class TestContentMaintenance:
    def test_absent_bootstrap_module_does_not_block(self, tmp_path, monkeypatch):
        """Content already in place needs no generation, so a missing bootstrap
        module must not stop the run."""
        import sys

        import tianluo.e2e as e2e_pkg

        # Simulate the module genuinely not being importable: drop the package
        # attribute (otherwise `from ...e2e import bootstrap` resolves it without
        # ever attempting an import) and poison the sys.modules entry.
        monkeypatch.delattr(e2e_pkg, "bootstrap", raising=False)
        monkeypatch.setitem(sys.modules, "tianluo.e2e.bootstrap", None)

        _write_config(tmp_path, enabled=True)
        step, flow = _step(), _flow(tmp_path)

        with patch("tianluo.e2e.session.run_e2e", return_value=_passing_verdict()):
            assert e2e_handler(step, flow) == StepStatus.COMPLETED

    def test_bootstrap_is_invoked_when_available(self, tmp_path, monkeypatch):
        calls = []

        class _Result:
            created = True

        def ensure_content(project_root, flow):
            calls.append((Path(project_root), flow))
            return _Result()

        _install_bootstrap(monkeypatch, ensure_content=ensure_content)

        _write_config(tmp_path, enabled=True)
        step, flow = _step(), _flow(tmp_path)

        with patch("tianluo.e2e.session.run_e2e", return_value=_passing_verdict()):
            assert e2e_handler(step, flow) == StepStatus.COMPLETED

        assert calls and calls[0][0] == tmp_path
        assert step.outputs["e2e_results"]["bootstrap"]

    def test_existing_content_is_evolved_not_left_alone(self, tmp_path, monkeypatch):
        """The regression this class exists for.

        With content already on disk ``ensure_content`` is a no-op, so a flow that
        only ever called it would re-run a stale suite forever: the endpoint this
        task added would never get a scenario, while the step still reported a
        green board.
        """
        evolve_calls = []

        _install_bootstrap(
            monkeypatch,
            ensure_content=lambda *a, **k: None,
            evolve_content=lambda root, flow, hints: evolve_calls.append(
                (Path(root), flow, list(hints))
            )
            or _EvolveResult(),
        )

        _write_config(tmp_path, enabled=True)
        step, flow = _step(), _flow(tmp_path)
        step.inputs["changes_made"] = {"files_changed": ["src/app/routes.py"]}

        with patch("tianluo.e2e.session.run_e2e", return_value=_passing_verdict()):
            assert e2e_handler(step, flow) == StepStatus.COMPLETED

        assert len(evolve_calls) == 1
        root, _, hints = evolve_calls[0]
        assert root == tmp_path
        joined = "\n".join(hints)
        assert "Add a login form" in joined  # the task description
        assert "src/app/routes.py" in joined  # what the implement step touched
        assert step.outputs["e2e_results"]["bootstrap"] == "evolved"

    def test_evolution_runs_before_the_scenarios(self, tmp_path, monkeypatch):
        """A scenario added this flow must be exercised by this flow.

        Evolving after the run would defer every new scenario by a whole flow, so
        a failure it exposes would never reach this task's fix loop.
        """
        order = []

        _install_bootstrap(
            monkeypatch,
            ensure_content=lambda *a, **k: None,
            evolve_content=lambda *a, **k: order.append("evolve") or _EvolveResult(),
        )

        _write_config(tmp_path, enabled=True)
        step, flow = _step(), _flow(tmp_path)
        step.inputs["changes_made"] = {"files_changed": ["src/app/routes.py"]}

        def _run(*args, **kwargs):
            order.append("run")
            return _passing_verdict()

        with patch("tianluo.e2e.session.run_e2e", side_effect=_run):
            e2e_handler(step, flow)

        assert order == ["evolve", "run"]

    def test_evolution_happens_once_per_flow(self, tmp_path, monkeypatch):
        """Fix-loop re-entry must not re-evolve.

        Beyond the wasted call per iteration, showing the model the assertion that
        is currently failing while asking it to revise scenarios invites making
        the suite pass by weakening it — the bypass the charter forbids.
        """
        evolve_calls = []

        _install_bootstrap(
            monkeypatch,
            ensure_content=lambda *a, **k: None,
            evolve_content=lambda *a, **k: evolve_calls.append(1) or _EvolveResult(),
        )

        _write_config(tmp_path, enabled=True)
        flow = _flow(tmp_path)

        for _ in range(3):
            step = _step()
            step.inputs["changes_made"] = {"files_changed": ["src/app/routes.py"]}
            with patch("tianluo.e2e.session.run_e2e", return_value=_failing_verdict()):
                assert e2e_handler(step, flow) == StepStatus.REVISION_NEEDED

        assert len(evolve_calls) == 1

    def test_first_generation_skips_evolution(self, tmp_path, monkeypatch):
        """Content authored against this very task has nothing to evolve yet."""
        evolve_calls = []

        class _Created:
            created = True

        _install_bootstrap(
            monkeypatch,
            ensure_content=lambda *a, **k: _Created(),
            evolve_content=lambda *a, **k: evolve_calls.append(1) or _EvolveResult(),
        )

        _write_config(tmp_path, enabled=True)
        step, flow = _step(), _flow(tmp_path)

        with patch("tianluo.e2e.session.run_e2e", return_value=_passing_verdict()):
            assert e2e_handler(step, flow) == StepStatus.COMPLETED

        assert evolve_calls == []
        # And the guard is set, so the next fix iteration does not read "never
        # maintained" and evolve content that is one step old.
        assert flow.state.context["e2e_content_evolved"] is True

    def test_rejected_evolution_does_not_break_the_run(self, tmp_path, monkeypatch):
        """The suite on disk is valid and runnable, so a bad proposal degrades."""
        _install_bootstrap(
            monkeypatch,
            ensure_content=lambda *a, **k: None,
            evolve_content=lambda *a, **k: _EvolveResult(
                changed=False, note="proposal rejected", errors=("driver 'web' unknown",)
            ),
        )

        _write_config(tmp_path, enabled=True)
        step, flow = _step(), _flow(tmp_path)
        step.inputs["changes_made"] = {"files_changed": ["src/app/routes.py"]}

        with patch("tianluo.e2e.session.run_e2e", return_value=_passing_verdict()):
            assert e2e_handler(step, flow) == StepStatus.COMPLETED

        assert step.outputs["e2e_results"]["bootstrap"] == "proposal rejected"

    def test_bootstrap_without_evolve_content_still_runs(self, tmp_path, monkeypatch):
        """An older/partial bootstrap module must not stop a runnable suite."""
        _install_bootstrap(monkeypatch, ensure_content=lambda *a, **k: None)

        _write_config(tmp_path, enabled=True)
        step, flow = _step(), _flow(tmp_path)
        step.inputs["changes_made"] = {"files_changed": ["src/app/routes.py"]}

        with patch("tianluo.e2e.session.run_e2e", return_value=_passing_verdict()):
            assert e2e_handler(step, flow) == StepStatus.COMPLETED

    def test_hints_cover_every_live_input_shape(self, tmp_path, monkeypatch):
        """The inputs the state machine actually forwards to this step.

        ``files_changed`` and ``implemented_groups`` each arrive either as bare
        strings (group-by-group execution) or as mappings (whole-plan execution),
        and the prose summary is a *sibling* key rather than part of
        ``changes_made`` — see ``_build_step_inputs``.
        """
        captured = []

        _install_bootstrap(
            monkeypatch,
            ensure_content=lambda *a, **k: None,
            evolve_content=lambda root, flow, hints: captured.append(list(hints))
            or _EvolveResult(),
        )

        _write_config(tmp_path, enabled=True)
        step, flow = _step(), _flow(tmp_path)
        step.inputs["changes_made"] = {
            "files_changed": [
                {"path": "src/app/routes.py", "action": "modified"},
                "src/app/models.py",
            ],
            "implemented_groups": [
                "G1",
                {"name": "G2", "description": "expose GET /health"},
            ],
        }
        step.inputs["implement_summary"] = "added GET /health"

        with patch("tianluo.e2e.session.run_e2e", return_value=_passing_verdict()):
            e2e_handler(step, flow)

        joined = "\n".join(captured[0])
        assert "src/app/routes.py" in joined
        assert "src/app/models.py" in joined
        assert "G1" in joined
        assert "expose GET /health" in joined
        assert "added GET /health" in joined

    def test_no_hints_means_no_evolution(self, tmp_path, monkeypatch):
        """An unaimed evolution pass is churn against a suite that already runs."""
        evolve_calls = []

        _install_bootstrap(
            monkeypatch,
            ensure_content=lambda *a, **k: None,
            evolve_content=lambda *a, **k: evolve_calls.append(1) or _EvolveResult(),
        )

        _write_config(tmp_path, enabled=True)
        step, flow = _step(), _flow(tmp_path)
        flow.task_description = ""

        with patch("tianluo.e2e.session.run_e2e", return_value=_passing_verdict()):
            assert e2e_handler(step, flow) == StepStatus.COMPLETED

        assert evolve_calls == []


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
