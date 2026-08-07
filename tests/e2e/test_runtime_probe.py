"""Tests for ``tianluo.e2e.runtime_probe`` — runtime selection and preflight.

Every case drives the probe through a stub ``runner`` instead of a real
``docker``/``podman`` binary, so the suite is fully deterministic and passes on
hosts with no container runtime installed at all.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tianluo.e2e import runtime_probe
from tianluo.e2e.errors import E2EConfigError, E2EEnvironmentError
from tianluo.e2e.runtime_probe import (
    RUNTIME_AUTO,
    RUNTIME_DOCKER,
    RUNTIME_PODMAN,
    RuntimeProbeResult,
    preflight,
    probe_one,
    probe_runtime,
)

DOCKER_INFO = json.dumps(
    {
        "ServerVersion": "26.1.0",
        "SecurityOptions": ["name=seccomp,profile=builtin"],
        "ClientInfo": {"Context": "default"},
    }
)
DOCKER_ROOTLESS_INFO = json.dumps(
    {
        "ServerVersion": "26.1.0",
        "SecurityOptions": ["name=seccomp,profile=builtin", "name=rootless"],
    }
)
PODMAN_INFO = json.dumps({"host": {"security": {"rootless": True}}})
PODMAN_ROOTFUL_INFO = json.dumps({"host": {"security": {"rootless": False}}})

# What `docker info` prints when the user is not in the `docker` group — the
# single most common "installed but unusable" state.
PERMISSION_DENIED = (
    "permission denied while trying to connect to the Docker daemon socket at "
    "unix:///var/run/docker.sock"
)


def ok(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def fail(stderr: str, code: int = 1) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=code, stdout="", stderr=stderr)


class StubRunner:
    """Stand-in for ``subprocess.run`` that records every invocation.

    ``outcomes`` maps a binary name to either a ``CompletedProcess`` or an
    exception instance to raise. A binary that is absent from the mapping
    behaves as "not installed" (``FileNotFoundError``), which is exactly how the
    OS reports it.
    """

    def __init__(self, outcomes: dict) -> None:
        self.outcomes = outcomes
        self.calls: list = []

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        outcome = self.outcomes.get(argv[0])
        if outcome is None:
            raise FileNotFoundError(2, "No such file or directory", argv[0])
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    @property
    def binaries(self) -> list:
        """The sequence of binaries the probe actually executed."""
        return [call[0] for call in self.calls]


class TestAutoSelection:
    def test_prefers_docker_when_both_are_usable(self):
        runner = StubRunner({"docker": ok(DOCKER_INFO), "podman": ok(PODMAN_INFO)})

        result = probe_runtime(RUNTIME_AUTO, runner=runner)

        assert result.name == RUNTIME_DOCKER
        assert result.ok is True
        # Podman is never probed once docker answers: the first success wins.
        assert runner.binaries == ["docker"]

    def test_falls_back_to_podman_when_docker_is_installed_but_denied(self):
        runner = StubRunner(
            {"docker": fail(PERMISSION_DENIED), "podman": ok(PODMAN_INFO)}
        )

        result = probe_runtime(RUNTIME_AUTO, runner=runner)

        # "Installed but unusable by this user" must count as absent.
        assert result.name == RUNTIME_PODMAN
        assert result.rootless is True
        assert runner.binaries == ["docker", "podman"]

    def test_falls_back_to_podman_when_docker_is_not_installed(self):
        runner = StubRunner({"podman": ok(PODMAN_INFO)})

        result = probe_runtime(RUNTIME_AUTO, runner=runner)

        assert result.name == RUNTIME_PODMAN
        assert runner.binaries == ["docker", "podman"]

    def test_falls_back_when_docker_info_times_out(self):
        runner = StubRunner(
            {
                "docker": subprocess.TimeoutExpired(cmd="docker info", timeout=30),
                "podman": ok(PODMAN_INFO),
            }
        )

        result = probe_runtime(RUNTIME_AUTO, runner=runner)

        assert result.name == RUNTIME_PODMAN

    def test_raises_with_all_three_repair_paths_when_none_available(self):
        runner = StubRunner({})

        with pytest.raises(E2EEnvironmentError) as excinfo:
            probe_runtime(RUNTIME_AUTO, runner=runner)

        message = str(excinfo.value)
        assert "docker" in message
        assert "podman" in message
        lowered = message.lower()
        # The three documented ways to get an unprivileged runtime working.
        assert "group" in lowered  # 1. join the docker group
        assert "install podman" in lowered  # 2. install podman
        assert "rootless" in lowered  # 3. rootless docker
        # Both candidates were tried before giving up.
        assert runner.binaries == ["docker", "podman"]
        assert excinfo.value.remediation

    def test_none_available_message_names_each_failed_candidate(self):
        runner = StubRunner({"docker": fail(PERMISSION_DENIED), "podman": fail("boom")})

        with pytest.raises(E2EEnvironmentError) as excinfo:
            probe_runtime(RUNTIME_AUTO, runner=runner)

        message = str(excinfo.value)
        assert "permission denied" in message
        assert "boom" in message


class TestExplicitSelectionNeverFallsBack:
    def test_explicit_docker_unavailable_does_not_try_podman(self):
        # Podman is perfectly usable here — the point is that it is never asked.
        runner = StubRunner({"podman": ok(PODMAN_INFO)})

        with pytest.raises(E2EEnvironmentError) as excinfo:
            probe_runtime(RUNTIME_DOCKER, runner=runner)

        assert runner.binaries == ["docker"]
        assert "docker" in str(excinfo.value)

    def test_explicit_podman_unavailable_does_not_try_docker(self):
        runner = StubRunner({"docker": ok(DOCKER_INFO)})

        with pytest.raises(E2EEnvironmentError) as excinfo:
            probe_runtime(RUNTIME_PODMAN, runner=runner)

        assert runner.binaries == ["podman"]
        assert "podman" in str(excinfo.value)

    def test_explicit_failure_carries_remediation_and_the_no_fallback_reason(self):
        runner = StubRunner({"podman": fail(PERMISSION_DENIED)})

        with pytest.raises(E2EEnvironmentError) as excinfo:
            probe_runtime(RUNTIME_PODMAN, runner=runner)

        assert "permission denied" in str(excinfo.value)
        assert "fall back" in str(excinfo.value).lower()
        assert "rootless" in excinfo.value.remediation.lower()

    def test_explicit_podman_selected_when_usable(self):
        runner = StubRunner({"docker": ok(DOCKER_INFO), "podman": ok(PODMAN_INFO)})

        result = probe_runtime(RUNTIME_PODMAN, runner=runner)

        assert result.name == RUNTIME_PODMAN
        assert runner.binaries == ["podman"]


class TestPreferenceNormalization:
    @pytest.mark.parametrize("preference", [None, "", "  ", "auto", "AUTO", " Auto "])
    def test_blank_and_cased_values_mean_auto(self, preference):
        runner = StubRunner({"docker": ok(DOCKER_INFO)})

        result = probe_runtime(preference, runner=runner)

        assert result.name == RUNTIME_DOCKER

    def test_case_and_whitespace_tolerated_for_explicit_runtime(self):
        runner = StubRunner({"podman": ok(PODMAN_INFO)})

        result = probe_runtime("  PODMAN ", runner=runner)

        assert result.name == RUNTIME_PODMAN
        assert runner.binaries == ["podman"]

    @pytest.mark.parametrize("preference", ["lxc", "containerd", 7, ["docker"]])
    def test_unknown_value_is_a_config_error_not_an_environment_error(self, preference):
        runner = StubRunner({"docker": ok(DOCKER_INFO)})

        with pytest.raises(E2EConfigError) as excinfo:
            probe_runtime(preference, runner=runner)

        assert "e2e.runtime" in str(excinfo.value)
        # A bad config value must not cause any probing at all.
        assert runner.calls == []


class TestProbeOne:
    def test_builds_a_json_formatted_info_command(self):
        runner = StubRunner({"docker": ok(DOCKER_INFO)})

        probe_one(RUNTIME_DOCKER, runner=runner)

        assert runner.calls[0] == ["docker", "info", "--format", "{{json .}}"]

    def test_never_raises_for_an_unusable_runtime(self):
        runner = StubRunner({})

        result = probe_one(RUNTIME_DOCKER, runner=runner)

        assert result.ok is False
        assert bool(result) is False
        assert result.error_key == "e2e.probe.not_installed"
        assert result.remediation

    def test_reports_exit_code_and_stderr_detail(self):
        runner = StubRunner({"docker": fail(PERMISSION_DENIED, code=125)})

        result = probe_one(RUNTIME_DOCKER, runner=runner)

        assert result.ok is False
        assert "125" in result.error
        assert "permission denied" in result.error

    def test_timeout_is_reported_as_its_own_failure_kind(self):
        runner = StubRunner(
            {"docker": subprocess.TimeoutExpired(cmd="docker info", timeout=30)}
        )

        result = probe_one(RUNTIME_DOCKER, runner=runner)

        assert result.ok is False
        assert result.error_key == "e2e.probe.timeout"

    def test_launch_failure_is_distinguished_from_a_nonzero_exit(self):
        runner = StubRunner({"docker": OSError("exec format error")})

        result = probe_one(RUNTIME_DOCKER, runner=runner)

        assert result.ok is False
        assert result.error_key == "e2e.probe.launch_failed"
        assert "exec format error" in result.error

    @pytest.mark.parametrize(
        "name,stdout,expected",
        [
            (RUNTIME_DOCKER, DOCKER_INFO, False),
            (RUNTIME_DOCKER, DOCKER_ROOTLESS_INFO, True),
            (RUNTIME_PODMAN, PODMAN_INFO, True),
            (RUNTIME_PODMAN, PODMAN_ROOTFUL_INFO, False),
        ],
    )
    def test_rootless_detection(self, name, stdout, expected):
        runner = StubRunner({name: ok(stdout)})

        assert probe_one(name, runner=runner).rootless is expected

    def test_unparsable_info_output_still_counts_as_usable(self):
        # A zero exit code is the authority on usability; JSON shape is only
        # consulted for the informational rootless flag.
        runner = StubRunner({"docker": ok("not json at all")})

        result = probe_one(RUNTIME_DOCKER, runner=runner)

        assert result.ok is True
        assert result.rootless is False


class TestResultObject:
    def test_result_is_immutable_so_a_session_can_pin_it(self):
        runner = StubRunner({"docker": ok(DOCKER_INFO)})
        result = probe_runtime(RUNTIME_AUTO, runner=runner)

        with pytest.raises(dataclasses.FrozenInstanceError):
            result.name = RUNTIME_PODMAN  # type: ignore[misc]

    def test_binary_matches_the_selected_runtime(self):
        runner = StubRunner({"podman": ok(PODMAN_INFO)})

        result = probe_runtime(RUNTIME_PODMAN, runner=runner)

        assert isinstance(result, RuntimeProbeResult)
        assert result.binary == RUNTIME_PODMAN


class TestPreflight:
    def test_shares_the_probe_path_with_probe_runtime(self):
        config = SimpleNamespace(enabled=True, runtime=RUNTIME_PODMAN)
        preflight_runner = StubRunner({"podman": ok(PODMAN_INFO)})
        probe_runner = StubRunner({"podman": ok(PODMAN_INFO)})

        from_preflight = preflight(config, runner=preflight_runner)
        from_probe = probe_runtime(RUNTIME_PODMAN, runner=probe_runner)

        assert from_preflight == from_probe
        assert preflight_runner.calls == probe_runner.calls

    def test_accepts_a_bare_preference_string(self):
        runner = StubRunner({"podman": ok(PODMAN_INFO)})

        assert preflight(RUNTIME_PODMAN, runner=runner).name == RUNTIME_PODMAN

    def test_no_config_means_auto(self):
        runner = StubRunner({"podman": ok(PODMAN_INFO)})

        assert preflight(None, runner=runner).name == RUNTIME_PODMAN
        assert runner.binaries == ["docker", "podman"]

    def test_config_without_runtime_attribute_means_auto(self):
        runner = StubRunner({"docker": ok(DOCKER_INFO)})

        assert preflight(SimpleNamespace(enabled=True), runner=runner).ok is True

    def test_failure_is_an_environment_error_so_it_skips_the_fix_loop(self):
        runner = StubRunner({})

        with pytest.raises(E2EEnvironmentError) as excinfo:
            preflight(SimpleNamespace(runtime=RUNTIME_AUTO), runner=runner)

        assert excinfo.value.remediation


def test_probe_module_never_escalates_privileges():
    """tianluo runs unprivileged end to end — no probe path may reach for root."""
    source = Path(runtime_probe.__file__).read_text(encoding="utf-8")
    assert "sudo" not in source
    assert "pkexec" not in source
