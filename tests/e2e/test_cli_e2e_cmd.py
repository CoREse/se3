"""Tests for the ``luo e2e`` command group.

Two things are worth pinning here. First, the **exit codes**: a script driving
``luo e2e run`` has to distinguish "the code under test is broken" (1) from
"this host cannot run containers" (3) from "the configuration is missing or
inadmissible" (4), and the three arrive by completely different routes. Second,
that the commands are **thin shells**: every one of them is asserted to reach the
same ``session.run_e2e`` / ``runtime_probe`` / ``bootstrap`` entry points the
engine's E2E step uses, because a second execution path would make the manual
command useless for debugging the automatic one.

Nothing here touches docker, podman or an LLM.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml
from typer.testing import CliRunner

from tianluo.commands.e2e_cmd import (
    EXIT_CONFIG,
    EXIT_ENVIRONMENT,
    EXIT_OK,
    EXIT_SCENARIO_FAILED,
    e2e_app,
)
from tianluo.e2e import bootstrap as bootstrap_module
from tianluo.e2e import runtime_probe as probe_module
from tianluo.e2e import session as session_module
from tianluo.e2e.errors import E2EConfigError, E2EEnvironmentError
from tianluo.e2e.executor import ScenarioResult
from tianluo.e2e.runtime_probe import RuntimeProbeResult
from tianluo.e2e.session import E2EVerdict

runner = CliRunner()


def output_of(result: Any) -> str:
    """Everything the command printed, on either stream."""
    text = result.stdout or ""
    try:
        text += result.stderr or ""
    except ValueError:  # pragma: no cover - depends on the click version
        pass
    return text


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project with e2e enabled and a valid content directory."""
    (tmp_path / "tianluo.yaml").write_text(
        yaml.safe_dump({"e2e": {"enabled": True}}, sort_keys=False), encoding="utf-8"
    )
    directory = tmp_path / "tianluo" / "e2e"
    (directory / "scenarios").mkdir(parents=True)
    (directory / "environment.yaml").write_text(
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
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (directory / "scenarios" / "cli-smoke.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "cli-smoke",
                "driver": "app",
                "actions": [{"action": "exec", "command": ["luo", "--version"]}],
                "assertions": [{"kind": "exit_code", "equals": 0}],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def usable_runtime(monkeypatch: pytest.MonkeyPatch) -> RuntimeProbeResult:
    """Make preflight report a healthy docker without running anything."""
    result = RuntimeProbeResult(name="docker", binary="docker", ok=True)
    monkeypatch.setattr(probe_module, "preflight", lambda *a, **k: result)
    return result


def passing_verdict(names: Optional[List[str]] = None) -> E2EVerdict:
    names = names or ["cli-smoke"]
    results = [ScenarioResult(name=name, passed=True) for name in names]
    return E2EVerdict(
        passed=True,
        scenario_results=results,
        summary={
            "runtime": "docker",
            "total": len(results),
            "passed": len(results),
            "failed": 0,
            "scenarios_passed": names,
            "scenarios_failed": [],
        },
    )


def failing_verdict() -> E2EVerdict:
    results = [ScenarioResult(name="cli-smoke", passed=False, driver="app")]
    return E2EVerdict(
        passed=False,
        scenario_results=results,
        fix_instructions="fix it",
        summary={
            "runtime": "docker",
            "total": 1,
            "passed": 0,
            "failed": 1,
            "scenarios_passed": [],
            "scenarios_failed": ["cli-smoke"],
        },
    )


def _add_visual_scenario(project: Path) -> None:
    """Add a tier-2 scenario whose baseline image has never been captured."""
    (project / "tianluo" / "e2e" / "scenarios" / "home.yaml").write_text(
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
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def stub_run(monkeypatch: pytest.MonkeyPatch, verdict: Any) -> Dict[str, Any]:
    """Replace ``session.run_e2e`` and record how the CLI called it."""
    recorded: Dict[str, Any] = {}

    def fake_run_e2e(root: Path, **kwargs: Any) -> Any:
        recorded["root"] = root
        recorded.update(kwargs)
        if isinstance(verdict, BaseException):
            raise verdict
        return verdict

    monkeypatch.setattr(session_module, "run_e2e", fake_run_e2e)
    return recorded


# ---------------------------------------------------------------------------
# luo e2e run
# ---------------------------------------------------------------------------


class TestRun:
    def test_all_passing_exits_zero(
        self, project: Path, usable_runtime: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub_run(monkeypatch, passing_verdict())

        result = runner.invoke(e2e_app, ["run", "-p", str(project)])

        assert result.exit_code == EXIT_OK
        assert "All e2e scenarios passed" in output_of(result)

    def test_a_failed_scenario_exits_one(
        self, project: Path, usable_runtime: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub_run(monkeypatch, failing_verdict())

        result = runner.invoke(e2e_app, ["run", "-p", str(project)])

        assert result.exit_code == EXIT_SCENARIO_FAILED
        assert "cli-smoke" in output_of(result)

    def test_the_failed_scenario_line_is_localized(
        self, project: Path, usable_runtime: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Console output goes through t() like every other line here.

        The per-scenario line used to be ``ScenarioResult.summary_line()`` — a
        hardcoded English string built for logs — so a zh-CN user got one English
        row interleaved with localized output.
        """
        from tianluo import i18n

        stub_run(monkeypatch, failing_verdict())
        monkeypatch.setenv("SE3_LANG", "zh-CN")
        i18n.reset_language()
        i18n.set_language("zh-CN")
        try:
            result = runner.invoke(e2e_app, ["run", "-p", str(project)])
        finally:
            i18n.set_language("en-US")

        text = output_of(result)
        assert "cli-smoke" in text
        assert "[PASS]" not in text and "[FAIL]" not in text
        assert "assertions" not in text

    def test_a_relative_project_root_reaches_the_session_absolute(
        self,
        project: Path,
        usable_runtime: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The root becomes the host side of every bind mount.

        docker/podman reject a relative ``-v .:/workspace`` as an invalid volume
        name, so `luo e2e run -p .` would fail to start a single container and
        report it as a host environment problem — for input the CLI accepted
        without complaint.
        """
        recorded = stub_run(monkeypatch, passing_verdict())
        monkeypatch.chdir(project)

        result = runner.invoke(e2e_app, ["run", "-p", "."])

        assert result.exit_code == EXIT_OK
        assert recorded["root"].is_absolute()
        assert recorded["root"] == project.resolve()
        assert recorded["content"].project_root.is_absolute()

    def test_a_preflight_failure_exits_with_the_environment_code(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The two non-zero codes must differ: a script may retry one, not the other."""

        def broken_preflight(*args: Any, **kwargs: Any) -> Any:
            raise E2EEnvironmentError(
                "no usable container runtime", remediation="add yourself to docker"
            )

        monkeypatch.setattr(probe_module, "preflight", broken_preflight)

        result = runner.invoke(e2e_app, ["run", "-p", str(project)])

        assert result.exit_code == EXIT_ENVIRONMENT
        assert result.exit_code != EXIT_SCENARIO_FAILED
        assert "add yourself to docker" in output_of(result)

    def test_an_environment_verdict_also_exits_with_the_environment_code(
        self, project: Path, usable_runtime: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run_e2e funnels host problems into a verdict as well as an exception."""
        stub_run(
            monkeypatch,
            E2EVerdict(
                passed=False,
                environment_error="db never became ready",
                remediation="check the readiness probe",
            ),
        )

        result = runner.invoke(e2e_app, ["run", "-p", str(project)])

        assert result.exit_code == EXIT_ENVIRONMENT
        assert "check the readiness probe" in output_of(result)

    def test_missing_content_exits_with_the_config_code(
        self, tmp_path: Path, usable_runtime: Any
    ) -> None:
        (tmp_path / "tianluo.yaml").write_text(
            yaml.safe_dump({"e2e": {"enabled": True}}), encoding="utf-8"
        )

        result = runner.invoke(e2e_app, ["run", "-p", str(tmp_path)])

        assert result.exit_code == EXIT_CONFIG
        assert "luo e2e bootstrap" in output_of(result)

    def test_inadmissible_content_exits_with_the_config_code(
        self, project: Path, usable_runtime: Any
    ) -> None:
        # Tier 2 without its opt-in: the ladder refuses it at load time.
        (project / "tianluo" / "e2e" / "scenarios" / "bad.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "bad",
                    "driver": "app",
                    "assertions": [
                        {"kind": "screenshot_diff", "baseline": "home.png"}
                    ],
                }
            ),
            encoding="utf-8",
        )

        result = runner.invoke(e2e_app, ["run", "-p", str(project)])

        assert result.exit_code == EXIT_CONFIG
        assert "visual_regression" in output_of(result)

    def test_scenario_selection_reaches_the_session(
        self, project: Path, usable_runtime: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded = stub_run(monkeypatch, passing_verdict(["a", "b"]))

        result = runner.invoke(
            e2e_app,
            ["run", "-p", str(project), "--scenario", "a", "--scenario", "b"],
        )

        assert result.exit_code == EXIT_OK
        assert recorded["scenarios"] == ["a", "b"]

    def test_keep_and_write_baselines_reach_the_session(
        self, project: Path, usable_runtime: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded = stub_run(monkeypatch, passing_verdict())

        result = runner.invoke(
            e2e_app, ["run", "-p", str(project), "--keep", "--write-baselines"]
        )

        assert result.exit_code == EXIT_OK
        assert recorded["keep_environment"] is True
        assert recorded["write_missing_baselines"] is True

    def test_write_baselines_gets_past_a_not_yet_captured_baseline(
        self, project: Path, usable_runtime: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The flag would be unreachable if loading rejected the absent image."""
        _add_visual_scenario(project)
        recorded = stub_run(monkeypatch, passing_verdict(["home"]))

        result = runner.invoke(
            e2e_app, ["run", "-p", str(project), "--write-baselines"]
        )

        assert result.exit_code == EXIT_OK, output_of(result)
        assert recorded["write_missing_baselines"] is True
        assert [s.name for s in recorded["content"].scenarios] == [
            "cli-smoke",
            "home",
        ]

    def test_without_the_flag_a_missing_baseline_is_a_configuration_error(
        self, project: Path, usable_runtime: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _add_visual_scenario(project)
        stub_run(monkeypatch, passing_verdict())

        result = runner.invoke(e2e_app, ["run", "-p", str(project)])

        assert result.exit_code == EXIT_CONFIG
        assert "home.png" in output_of(result)

    def test_a_kept_environment_prints_the_cleanup_commands(
        self, project: Path, usable_runtime: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Logging is not an option here: `luo` installs no logging handler."""
        verdict = passing_verdict()
        verdict.notices.append("docker rm -f app && docker network rm tianluo-e2e")
        stub_run(monkeypatch, verdict)

        result = runner.invoke(e2e_app, ["run", "-p", str(project), "--keep"])

        assert result.exit_code == EXIT_OK
        assert "docker network rm tianluo-e2e" in output_of(result)

    def test_without_keep_the_configured_value_still_decides(
        self, project: Path, usable_runtime: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--keep`` can only turn keeping on; absence must not force it off."""
        recorded = stub_run(monkeypatch, passing_verdict())

        runner.invoke(e2e_app, ["run", "-p", str(project)])

        assert recorded["keep_environment"] is None

    def test_the_probe_result_is_handed_on_rather_than_probed_twice(
        self, project: Path, usable_runtime: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded = stub_run(monkeypatch, passing_verdict())

        runner.invoke(e2e_app, ["run", "-p", str(project)])

        assert recorded["probe"] is usable_runtime

    def test_a_disabled_project_is_told_so_but_still_runs(
        self, project: Path, usable_runtime: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Manual invocation IS the intent the switch encodes for the flow."""
        (project / "tianluo.yaml").write_text(
            yaml.safe_dump({"e2e": {"enabled": False}}), encoding="utf-8"
        )
        stub_run(monkeypatch, passing_verdict())

        result = runner.invoke(e2e_app, ["run", "-p", str(project)])

        assert result.exit_code == EXIT_OK
        assert "e2e.enabled" in output_of(result)


# ---------------------------------------------------------------------------
# luo e2e list
# ---------------------------------------------------------------------------


class TestList:
    def test_lists_services_and_scenarios(self, project: Path) -> None:
        result = runner.invoke(e2e_app, ["list", "-p", str(project)])

        assert result.exit_code == EXIT_OK
        text = output_of(result)
        assert "app" in text
        assert "python:3.12-slim" in text
        assert "cli-smoke" in text

    def test_needs_no_container_runtime(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Listing must work on a host that cannot run containers at all."""

        def explode(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover
            raise AssertionError("list must not probe the container runtime")

        monkeypatch.setattr(probe_module, "preflight", explode)
        monkeypatch.setattr(probe_module, "probe_one", explode)

        result = runner.invoke(e2e_app, ["list", "-p", str(project)])

        assert result.exit_code == EXIT_OK

    def test_missing_content_exits_with_the_config_code(
        self, tmp_path: Path
    ) -> None:
        result = runner.invoke(e2e_app, ["list", "-p", str(tmp_path)])

        assert result.exit_code == EXIT_CONFIG


# ---------------------------------------------------------------------------
# luo e2e doctor
# ---------------------------------------------------------------------------


def stub_probe(
    monkeypatch: pytest.MonkeyPatch, outcomes: Dict[str, bool]
) -> List[str]:
    """Answer ``probe_one`` from a name -> usable map, recording the order."""
    probed: List[str] = []

    def fake_probe_one(name: str, **kwargs: Any) -> RuntimeProbeResult:
        probed.append(name)
        if outcomes.get(name):
            return RuntimeProbeResult(name=name, binary=name, ok=True)
        from tianluo.i18n import t

        return RuntimeProbeResult(
            name=name,
            binary=name,
            ok=False,
            error="{} is not usable".format(name),
            remediation=t("e2e.probe.remediation"),
        )

    monkeypatch.setattr(probe_module, "probe_one", fake_probe_one)
    return probed


class TestDoctor:
    def test_reports_the_usable_runtime_and_exits_zero(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub_probe(monkeypatch, {"docker": True, "podman": False})

        result = runner.invoke(e2e_app, ["doctor", "-p", str(project)])

        assert result.exit_code == EXIT_OK
        assert "docker" in output_of(result)

    def test_prints_the_three_repair_routes_and_exits_non_zero(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No runtime is an environment problem, and the fix menu must be printed."""
        stub_probe(monkeypatch, {"docker": False, "podman": False})

        result = runner.invoke(e2e_app, ["doctor", "-p", str(project)])

        assert result.exit_code == EXIT_ENVIRONMENT
        text = output_of(result)
        assert "docker" in text and "group" in text
        assert "podman" in text
        assert "rootless" in text.lower()

    def test_auto_reports_every_candidate(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A diagnostic that stopped at the first success would hide the picture."""
        probed = stub_probe(monkeypatch, {"docker": True, "podman": True})

        runner.invoke(e2e_app, ["doctor", "-p", str(project)])

        assert probed == ["docker", "podman"]

    def test_an_explicit_runtime_is_probed_alone(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No silent fallback: reporting on the other runtime would mislead."""
        (project / "tianluo.yaml").write_text(
            yaml.safe_dump({"e2e": {"enabled": True, "runtime": "podman"}}),
            encoding="utf-8",
        )
        probed = stub_probe(monkeypatch, {"docker": True, "podman": False})

        result = runner.invoke(e2e_app, ["doctor", "-p", str(project)])

        assert probed == ["podman"]
        assert result.exit_code == EXIT_ENVIRONMENT

    def test_creates_nothing(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub_probe(monkeypatch, {"docker": True, "podman": True})
        before = sorted(str(p) for p in project.rglob("*"))

        runner.invoke(e2e_app, ["doctor", "-p", str(project)])

        assert sorted(str(p) for p in project.rglob("*")) == before


# ---------------------------------------------------------------------------
# luo e2e bootstrap
# ---------------------------------------------------------------------------


class TestBootstrap:
    def test_reports_what_it_generated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            bootstrap_module,
            "ensure_content",
            lambda root, flow=None, **kw: bootstrap_module.BootstrapResult(
                created=True, written=("tianluo/e2e/environment.yaml",)
            ),
        )

        result = runner.invoke(e2e_app, ["bootstrap", "-p", str(tmp_path)])

        assert result.exit_code == EXIT_OK
        assert "tianluo/e2e/environment.yaml" in output_of(result)

    def test_says_so_when_there_was_nothing_to_do(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            bootstrap_module,
            "ensure_content",
            lambda root, flow=None, **kw: bootstrap_module.BootstrapResult(),
        )

        result = runner.invoke(e2e_app, ["bootstrap", "-p", str(tmp_path)])

        assert result.exit_code == EXIT_OK
        assert "already present" in output_of(result)

    def test_hints_route_to_incremental_evolution(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded: Dict[str, Any] = {}

        def fake_evolve(root: Path, flow: Any = None, hints: Any = None, **kw: Any):
            recorded["hints"] = list(hints or [])
            return bootstrap_module.BootstrapResult(
                evolved=True, written=("tianluo/e2e/scenarios/new.yaml",)
            )

        monkeypatch.setattr(bootstrap_module, "evolve_content", fake_evolve)
        monkeypatch.setattr(
            bootstrap_module,
            "ensure_content",
            lambda *a, **k: pytest.fail("hints must not take the generation path"),
        )

        result = runner.invoke(
            e2e_app,
            ["bootstrap", "-p", str(project), "--hint", "cover /health"],
        )

        assert result.exit_code == EXIT_OK
        assert recorded["hints"] == ["cover /health"]

    def test_a_generation_failure_exits_with_the_config_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explode(*args: Any, **kwargs: Any) -> Any:
            raise E2EConfigError("the model produced nothing admissible")

        monkeypatch.setattr(bootstrap_module, "ensure_content", explode)

        result = runner.invoke(e2e_app, ["bootstrap", "-p", str(tmp_path)])

        assert result.exit_code == EXIT_CONFIG
        assert "nothing admissible" in output_of(result)

    def test_a_discarded_evolution_is_reported_but_not_an_error(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The suite on disk is still valid, so a bad suggestion is not a failure."""
        monkeypatch.setattr(
            bootstrap_module,
            "evolve_content",
            lambda *a, **k: bootstrap_module.BootstrapResult(
                errors=("driver names an undeclared service",)
            ),
        )

        result = runner.invoke(
            e2e_app, ["bootstrap", "-p", str(project), "--hint", "x"]
        )

        assert result.exit_code == EXIT_OK
        assert "undeclared service" in output_of(result)


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------


class TestWiring:
    def test_every_subcommand_is_registered_and_documented(self) -> None:
        result = runner.invoke(e2e_app, ["--help"])

        assert result.exit_code == 0
        for name in ("run", "list", "doctor", "bootstrap"):
            assert name in result.stdout

    def test_the_group_is_reachable_from_the_root_cli(self) -> None:
        from tianluo import cli

        result = runner.invoke(cli.app, ["e2e", "--help"])

        assert result.exit_code == 0

    def test_the_module_defers_every_e2e_import(self) -> None:
        """A module-level e2e import would put the extra on the core CLI path."""
        from tianluo.commands import e2e_cmd

        source = Path(e2e_cmd.__file__).read_text(encoding="utf-8")
        for line in source.splitlines():
            if line.startswith("from ..e2e") or line.startswith("import ..e2e"):
                raise AssertionError("module-level e2e import: " + line)

    def test_the_exit_codes_stay_distinct(self) -> None:
        """Click owns 2 for usage errors, so the three outcomes need 0/1/3/4."""
        codes = [EXIT_OK, EXIT_SCENARIO_FAILED, EXIT_ENVIRONMENT, EXIT_CONFIG]

        assert len(set(codes)) == len(codes)
        assert 2 not in codes
