"""Tests for the scenario executor.

Driven entirely by the shared :class:`FakeBackend` and a :class:`FakeClock`, so
no container starts and no wall-clock second passes: the time budget is
exercised by making the injected clock jump, which is the only way to test a
timeout deterministically.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tianluo.e2e.backend import ExecResult
from tianluo.e2e.errors import E2EConfigError
from tianluo.e2e.executor import Executor, ScenarioResult, run_scenarios

from ._stubs import (
    FakeBackend,
    FakeClock,
    action,
    assertion,
    content,
    marked,
    scenario,
    service_decl,
    write_png,
)


class Config:
    """Minimal stand-in for E2EConfig's executor-facing surface."""

    def __init__(self, scenario_timeout: int = 300) -> None:
        self.scenario_timeout = scenario_timeout


def build(tmp_path, backend, *, services=None, scenarios=(), **kwargs):
    bundle = content(tmp_path, services=services, scenarios=scenarios)
    handle = backend.create(bundle.to_environment_spec())
    kwargs.setdefault("config", Config())
    config = kwargs.pop("config")
    return Executor(backend, bundle, config, handle=handle, **kwargs), bundle


# ---------------------------------------------------------------------------
# driver resolution
# ---------------------------------------------------------------------------


class TestDriverResolution:
    def test_application_container_is_its_own_driver(self, tmp_path):
        backend = FakeBackend()
        executor, _ = build(
            tmp_path, backend, services=(service_decl("app"),)
        )

        target = executor.resolve_driver(scenario("cli", driver="app"))

        assert target.service == "app"
        assert target.is_browser is False

    def test_playwright_service_is_a_browser_driver(self, tmp_path):
        backend = FakeBackend()
        executor, _ = build(
            tmp_path,
            backend,
            services=(
                service_decl("app"),
                service_decl(
                    "driver",
                    image="mcr.microsoft.com/playwright:v1.44.0",
                    base_kind="playwright",
                ),
            ),
        )

        target = executor.resolve_driver(scenario("web", driver="driver"))

        assert target.service == "driver"
        assert target.is_browser is True
        assert target.base_kind == "playwright"

    def test_undeclared_driver_is_a_config_error(self, tmp_path):
        backend = FakeBackend()
        executor, _ = build(tmp_path, backend, services=(service_decl("app"),))

        with pytest.raises(E2EConfigError) as excinfo:
            executor.resolve_driver(scenario("web", driver="ghost"))

        assert "ghost" in str(excinfo.value)
        assert "app" in str(excinfo.value)

    def test_running_without_an_environment_is_refused(self, tmp_path):
        backend = FakeBackend()
        bundle = content(tmp_path)
        executor = Executor(backend, bundle, Config())

        with pytest.raises(E2EConfigError):
            executor.run_scenario(scenario(assertions=(assertion("exit_code"),)))


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------


class TestActions:
    def test_exec_list_command_passes_argv_through(self, tmp_path):
        backend = FakeBackend()
        executor, _ = build(tmp_path, backend)

        result = executor.run_scenario(
            scenario(
                actions=(action("exec", command=["luo", "--version"]),),
                assertions=(assertion("exit_code"),),
            )
        )

        assert result.passed
        assert backend.exec_calls[0][1] == ["luo", "--version"]

    def test_exec_string_command_keeps_its_shell(self, tmp_path):
        backend = FakeBackend()
        executor, _ = build(tmp_path, backend)

        executor.run_scenario(
            scenario(
                actions=(action("exec", command="luo --version | head -1"),),
                assertions=(assertion("exit_code"),),
            )
        )

        assert backend.exec_calls[0][1] == ["sh", "-lc", "luo --version | head -1"]

    def test_a_quoted_action_timeout_is_coerced(self, tmp_path):
        """Machine-written YAML quotes numbers; that must not reach arithmetic raw."""
        backend = FakeBackend()
        executor, _ = build(tmp_path, backend)

        result = executor.run_scenario(
            scenario(
                actions=(action("exec", command=["luo"], timeout="5"),),
                assertions=(assertion("exit_code"),),
            )
        )

        assert result.passed
        assert backend.exec_calls[0][2]["timeout"] == 5.0

    @pytest.mark.parametrize("bad", ["fast", 0, -3])
    def test_a_non_numeric_action_timeout_is_a_located_config_error(
        self, tmp_path, bad
    ):
        backend = FakeBackend()
        executor, _ = build(tmp_path, backend)

        with pytest.raises(E2EConfigError) as excinfo:
            executor.run_scenario(
                scenario(
                    name="smoke",
                    actions=(action("exec", command=["luo"], timeout=bad),),
                    assertions=(assertion("exit_code"),),
                )
            )

        assert "smoke" in str(excinfo.value)

    def test_a_non_numeric_wait_interval_is_a_located_config_error(self, tmp_path):
        backend = FakeBackend(exec_results=[ExecResult(exit_code=0)])
        executor, _ = build(tmp_path, backend)

        with pytest.raises(E2EConfigError):
            executor.run_scenario(
                scenario(
                    actions=(
                        action("wait", until=["test", "-e", "/tmp/x"], interval="soon"),
                    ),
                    assertions=(assertion("exit_code"),),
                )
            )

    def test_exec_honours_service_workdir_and_environment(self, tmp_path):
        backend = FakeBackend()
        executor, _ = build(
            tmp_path, backend, services=(service_decl("app"), service_decl("db"))
        )

        executor.run_scenario(
            scenario(
                actions=(
                    action(
                        "exec",
                        command=["psql", "-c", "select 1"],
                        service="db",
                        workdir="/data",
                        environment={"PGUSER": "test"},
                    ),
                ),
                assertions=(assertion("exit_code"),),
            )
        )

        service, argv, kwargs = backend.exec_calls[0]
        assert service == "db"
        assert kwargs["workdir"] == "/data"
        assert kwargs["environment"] == {"PGUSER": "test"}

    def test_failing_action_does_not_abort_the_scenario(self, tmp_path):
        backend = FakeBackend(
            exec_results=[
                ExecResult(exit_code=1, stderr="boom"),
                ExecResult(exit_code=0, stdout="recovered"),
            ]
        )
        executor, _ = build(tmp_path, backend)

        result = executor.run_scenario(
            scenario(
                actions=(
                    action("exec", command=["broken"]),
                    action("exec", command=["retry"]),
                ),
                assertions=(assertion("stdout", contains="recovered"),),
            )
        )

        assert result.passed
        assert len(backend.exec_calls) == 2
        assert any("exited 1" in note for note in result.evidence)
        # A non-zero exec is a note, not a verdict: the exit_code / stdout /
        # stderr assertions are what judge a command. It still has to reach the
        # fix loop, which reads `notes`.
        assert any("exited 1" in note for note in result.notes)
        assert result.action_failures == []

    def test_an_unreachable_http_action_fails_the_scenario(
        self, tmp_path, monkeypatch
    ):
        """No assertion adjudicates a driving request: `http_status` re-fetches."""
        import urllib.error
        import urllib.request

        def refuse(url, timeout=None):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", refuse)
        executor, _ = build(tmp_path, FakeBackend())

        result = executor.run_scenario(
            scenario(
                actions=(action("http", url="http://127.0.0.1:1/gone"),),
                assertions=(assertion("file_exists", path="/pre/existing"),),
            )
        )

        assert result.assertions[0].passed
        assert not result.passed
        assert any("action http" in failure for failure in result.action_failures)

    def test_empty_command_is_a_config_error(self, tmp_path):
        executor, _ = build(tmp_path, FakeBackend())

        with pytest.raises(E2EConfigError):
            executor.run_scenario(
                scenario(
                    actions=(action("exec", command=""),),
                    assertions=(assertion("exit_code"),),
                )
            )

    def test_unknown_action_is_a_config_error(self, tmp_path):
        executor, _ = build(tmp_path, FakeBackend())

        with pytest.raises(E2EConfigError) as excinfo:
            executor.run_scenario(
                scenario(
                    actions=(action("teleport", to="prod"),),
                    assertions=(assertion("exit_code"),),
                )
            )

        assert "teleport" in str(excinfo.value)

    def test_wait_seconds_uses_the_injected_sleeper(self, tmp_path):
        backend = FakeBackend()
        clock = FakeClock()
        executor, _ = build(
            tmp_path, backend, clock=clock, sleeper=clock.sleep
        )

        executor.run_scenario(
            scenario(
                actions=(action("wait", seconds=2),),
                assertions=(assertion("file_exists", path="/x"),),
            )
        )

        assert clock.slept == [2.0]

    def test_wait_until_polls_until_the_command_succeeds(self, tmp_path):
        backend = FakeBackend(
            exec_results=[
                ExecResult(exit_code=1),
                ExecResult(exit_code=1),
                ExecResult(exit_code=0),
                ExecResult(exit_code=0),
            ]
        )
        clock = FakeClock()
        executor, _ = build(tmp_path, backend, clock=clock, sleeper=clock.sleep)

        result = executor.run_scenario(
            scenario(
                actions=(
                    action("wait", until=["pgrep", "server"]),
                    action("exec", command=["run"]),
                ),
                assertions=(assertion("exit_code"),),
            )
        )

        assert result.passed
        assert len(backend.argv_containing("pgrep")) == 3

    def test_poll_probe_is_not_the_exec_an_assertion_describes(self, tmp_path):
        """A `wait.until` probe must not become the scenario's `last_exec`.

        The probe necessarily ends up exiting 0 (that is what stops the poll), so
        letting it take the slot would report a green exit code for whatever the
        scenario's own command did.
        """
        backend = FakeBackend(
            exec_results=[
                ExecResult(exit_code=3, stdout="broke"),  # the declared exec
                ExecResult(exit_code=0),  # the wait.until probe
            ]
        )
        clock = FakeClock()
        executor, _ = build(tmp_path, backend, clock=clock, sleeper=clock.sleep)

        result = executor.run_scenario(
            scenario(
                actions=(
                    action("exec", command=["run-me"]),
                    action("wait", until=["pgrep", "server"]),
                ),
                assertions=(assertion("exit_code", equals=0),),
            )
        )

        assert not result.passed
        assert "exit_code == 3" in result.assertions[0].actual

    def test_screenshot_action_records_an_artifact(self, tmp_path):
        shot = write_png(tmp_path / "src.png")
        backend = FakeBackend(screenshot_bytes=shot.read_bytes())
        executor, _ = build(
            tmp_path, backend, artifacts_dir=tmp_path / "artifacts"
        )

        result = executor.run_scenario(
            scenario(
                actions=(action("screenshot", name="home"),),
                assertions=(assertion("file_exists", path="/x"),),
            )
        )

        assert backend.snapshot_calls[0][2] == "screenshot"
        assert len(result.artifacts) == 1
        assert Path(result.artifacts[0]).is_file()

    def test_visual_click_requires_the_declaration(self, tmp_path):
        executor, _ = build(tmp_path, FakeBackend())

        with pytest.raises(E2EConfigError) as excinfo:
            executor.run_scenario(
                scenario(
                    actions=(action("visual_click", x=10, y=20),),
                    assertions=(assertion("exit_code"),),
                )
            )

        assert "visual_driving" in str(excinfo.value)

    def test_declared_visual_click_drives_xdotool(self, tmp_path):
        backend = FakeBackend()
        executor, _ = build(tmp_path, backend)

        executor.run_scenario(
            scenario(
                actions=(action("visual_click", x=10, y=20),),
                assertions=(assertion("exit_code"),),
                visual_driving=True,
            )
        )

        assert backend.exec_calls[0][1] == [
            "xdotool", "mousemove", "10", "20", "click", "1",
        ]


# ---------------------------------------------------------------------------
# browser scenarios
# ---------------------------------------------------------------------------


def browser_services():
    return (
        service_decl("app"),
        service_decl(
            "driver",
            image="mcr.microsoft.com/playwright:v1.44.0",
            base_kind="playwright",
        ),
    )


class TestBrowserScenarios:
    def test_actions_and_queries_run_as_one_program(self, tmp_path):
        backend = FakeBackend(
            exec_handler=lambda service, argv: ExecResult(
                exit_code=0,
                stdout=marked(
                    {
                        "ops": [
                            {"op": "goto", "ok": True},
                            {"op": "fill", "ok": True},
                            {"op": "click", "ok": True},
                        ],
                        "queries": [
                            {"count": 1, "text": "Welcome alice"},
                            {"count": 0},
                        ],
                    }
                ),
            )
        )
        executor, _ = build(tmp_path, backend, services=browser_services())

        result = executor.run_scenario(
            scenario(
                "login",
                driver="driver",
                actions=(
                    action("browser", op="goto", url="http://app:8000/login"),
                    action("browser", op="fill", selector="#user", value="alice"),
                    action("browser", op="click", selector="button"),
                ),
                assertions=(
                    assertion("dom", selector=".greeting", contains="alice"),
                    assertion("dom", selector=".error", absent=True),
                ),
            )
        )

        assert result.passed, [a.actual for a in result.assertions]
        # One exec for the whole scenario: the login form is submitted once.
        assert len(backend.exec_calls) == 1
        program = backend.exec_calls[0][1][2]
        assert program.index("http://app:8000/login") < program.index("alice")

    def test_browser_screenshot_op_records_its_in_container_path(self, tmp_path):
        recorded = {}

        def handler(service, argv):
            recorded["program"] = argv[2]
            return ExecResult(
                exit_code=0,
                stdout=marked(
                    {"ops": [{"op": "screenshot", "ok": True}], "queries": [{"count": 1}]}
                ),
            )

        backend = FakeBackend(exec_handler=handler)
        executor, _ = build(tmp_path, backend, services=browser_services())

        result = executor.run_scenario(
            scenario(
                driver="driver",
                actions=(action("browser", op="screenshot", name="home"),),
                assertions=(assertion("dom", selector="h1"),),
            )
        )

        assert result.passed
        assert "/tmp/tianluo-e2e-home.png" in recorded["program"]
        assert any("browser screenshot home" in note for note in result.evidence)

    def test_browser_action_needs_a_playwright_driver(self, tmp_path):
        executor, _ = build(tmp_path, FakeBackend(), services=(service_decl("app"),))

        with pytest.raises(E2EConfigError) as excinfo:
            executor.run_scenario(
                scenario(
                    actions=(action("browser", op="goto", url="http://app/"),),
                    assertions=(assertion("dom", selector="h1"),),
                )
            )

        assert "playwright" in str(excinfo.value)

    def test_unknown_browser_op_is_a_config_error(self, tmp_path):
        executor, _ = build(tmp_path, FakeBackend(), services=browser_services())

        with pytest.raises(E2EConfigError) as excinfo:
            executor.run_scenario(
                scenario(
                    driver="driver",
                    actions=(action("browser", op="hypnotise", selector="#x"),),
                    assertions=(assertion("dom", selector="h1"),),
                )
            )

        assert "hypnotise" in str(excinfo.value)

    def test_failed_browser_op_is_surfaced_on_the_result(self, tmp_path):
        backend = FakeBackend(
            exec_handler=lambda service, argv: ExecResult(
                exit_code=0,
                stdout=marked(
                    {
                        "ops": [
                            {"op": "click", "ok": False, "error": "selector not found"}
                        ],
                        "queries": [{"count": 0}],
                    }
                ),
            )
        )
        executor, _ = build(tmp_path, backend, services=browser_services())

        result = executor.run_scenario(
            scenario(
                driver="driver",
                actions=(action("browser", op="click", selector="#gone"),),
                assertions=(assertion("dom", selector=".greeting"),),
            )
        )

        assert not result.passed
        assert "selector not found" in result.action_failures[0]

    def test_a_failed_browser_op_condemns_even_a_green_assertion_set(self, tmp_path):
        """A click that never landed leaves the behaviour under test unexercised.

        The assertions here are deliberately unrelated to the UI (a file that was
        already there), which is exactly the shape that used to report PASSED.
        """
        backend = FakeBackend(
            exec_handler=lambda service, argv: ExecResult(
                exit_code=0,
                stdout=marked(
                    {
                        "ops": [
                            {"op": "click", "ok": False, "error": "selector not found"}
                        ],
                        "queries": [],
                    }
                )
                if argv and argv[0] == "node"
                else "",
            )
        )
        executor, _ = build(tmp_path, backend, services=browser_services())

        result = executor.run_scenario(
            scenario(
                driver="driver",
                actions=(action("browser", op="click", selector="#gone"),),
                assertions=(assertion("file_exists", path="/pre/existing"),),
            )
        )

        assert result.assertions[0].passed
        assert not result.passed

    def test_a_non_browser_action_after_a_browser_one_is_refused(self, tmp_path):
        """The declared order could not be honoured, so the document is rejected.

        Browser ops are batched into one program that runs last; an `exec`
        declared after them would in fact execute first.
        """
        executor, _ = build(tmp_path, FakeBackend(), services=browser_services())

        with pytest.raises(E2EConfigError) as excinfo:
            executor.run_scenario(
                scenario(
                    driver="driver",
                    actions=(
                        action("browser", op="goto", url="http://app/"),
                        action("exec", command=["seed"]),
                    ),
                    assertions=(assertion("dom", selector="h1"),),
                )
            )

        assert "browser" in str(excinfo.value)


# ---------------------------------------------------------------------------
# assertion collection and time budget
# ---------------------------------------------------------------------------


class TestAssertionCollection:
    def test_every_assertion_is_evaluated_by_default(self, tmp_path):
        backend = FakeBackend(
            exec_results=[ExecResult(exit_code=1, stdout="nope", stderr="bad")]
        )
        executor, _ = build(tmp_path, backend)

        result = executor.run_scenario(
            scenario(
                actions=(action("exec", command=["run"]),),
                assertions=(
                    assertion("exit_code", equals=0),
                    assertion("stdout", contains="ready"),
                    assertion("stderr", contains="bad"),
                ),
            )
        )

        assert not result.passed
        # All three evaluated: one round must expose the whole failure surface.
        assert len(result.assertions) == 3
        assert [a.passed for a in result.assertions] == [False, False, True]
        assert len(result.failed_assertions) == 2

    def test_fail_fast_stops_after_the_first_failure(self, tmp_path):
        backend = FakeBackend(exec_results=[ExecResult(exit_code=1)])
        executor, _ = build(tmp_path, backend)

        result = executor.run_scenario(
            scenario(
                actions=(action("exec", command=["run"]),),
                assertions=(
                    assertion("exit_code", equals=0),
                    assertion("stdout", contains="ready"),
                ),
                fail_fast=True,
            )
        )

        assert len(result.assertions) == 1
        assert any("fail-fast" in note for note in result.evidence)

    def test_scenario_with_no_passing_assertion_is_not_green(self, tmp_path):
        executor, _ = build(tmp_path, FakeBackend())

        result = executor.run_scenario(scenario(assertions=()))

        assert not result.passed

    def test_result_carries_every_field(self, tmp_path):
        shot = write_png(tmp_path / "src.png")
        backend = FakeBackend(screenshot_bytes=shot.read_bytes())
        executor, _ = build(
            tmp_path, backend, artifacts_dir=tmp_path / "artifacts"
        )

        result = executor.run_scenario(
            scenario(
                "smoke",
                actions=(
                    action("exec", command=["run"]),
                    action("screenshot", name="after"),
                ),
                assertions=(assertion("exit_code"),),
            )
        )

        assert isinstance(result, ScenarioResult)
        assert result.name == "smoke"
        assert result.passed is True
        assert result.driver == "app"
        assert result.source.endswith("smoke.yaml")
        assert result.duration >= 0.0
        assert result.assertions and result.artifacts
        assert isinstance(result.evidence, list)
        assert result.summary_line().startswith("[PASS] smoke")

        payload = result.to_dict()
        assert payload["assertions"][0]["kind"] == "exit_code"
        assert payload["passed"] is True

    def test_run_scenarios_preserves_declaration_order(self, tmp_path):
        backend = FakeBackend()
        executor, _ = build(tmp_path, backend)
        scenarios = [
            scenario("first", assertions=(assertion("file_exists", path="/a"),)),
            scenario("second", assertions=(assertion("file_exists", path="/b"),)),
        ]

        results = run_scenarios(executor, scenarios)

        assert [r.name for r in results] == ["first", "second"]


class TestTimeBudget:
    def test_scenario_timeout_produces_a_result_not_an_exception(self, tmp_path):
        clock = FakeClock()

        def burn(service, argv):
            clock.now += 40.0
            return ExecResult(exit_code=0)

        backend = FakeBackend(exec_handler=burn)
        executor, _ = build(
            tmp_path,
            backend,
            config=Config(scenario_timeout=60),
            clock=clock,
            sleeper=clock.sleep,
        )

        result = executor.run_scenario(
            scenario(
                actions=(
                    action("exec", command=["slow-1"]),
                    action("exec", command=["slow-2"]),
                    action("exec", command=["slow-3"]),
                ),
                assertions=(assertion("exit_code"),),
            )
        )

        assert result.timed_out is True
        assert not result.passed
        assert "budget" in result.error
        # The third action never started: the budget was already spent.
        assert len(backend.exec_calls) == 2

    def test_scenario_timeout_overrides_the_config_default(self, tmp_path):
        backend = FakeBackend()
        executor, _ = build(tmp_path, backend, config=Config(scenario_timeout=300))

        assert executor.scenario_budget(scenario(timeout=30)) == 30.0
        assert executor.scenario_budget(scenario()) == 300.0

    def test_per_call_timeout_is_clamped_to_the_remaining_budget(self, tmp_path):
        clock = FakeClock()
        backend = FakeBackend()
        executor, _ = build(
            tmp_path, backend, config=Config(scenario_timeout=10), clock=clock,
            sleeper=clock.sleep,
        )

        executor.run_scenario(
            scenario(
                actions=(action("exec", command=["run"], timeout=999),),
                assertions=(assertion("exit_code"),),
            )
        )

        assert backend.exec_calls[0][2]["timeout"] == 10.0

    def test_assertions_are_skipped_once_the_budget_is_gone(self, tmp_path):
        clock = FakeClock()

        def burn(service, argv):
            clock.now += 100.0
            return ExecResult(exit_code=0)

        backend = FakeBackend(exec_handler=burn)
        executor, _ = build(
            tmp_path, backend, config=Config(scenario_timeout=50), clock=clock,
            sleeper=clock.sleep,
        )

        result = executor.run_scenario(
            scenario(
                actions=(action("exec", command=["slow"]),),
                assertions=(assertion("exit_code"), assertion("stdout", contains="x")),
            )
        )

        assert result.timed_out
        assert result.assertions == []
