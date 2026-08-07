"""Tests for ``tianluo.e2e.container_backend``.

None of these need a container runtime: the backend's subprocess entry point is
injected, so the assertions target the argv each verb assembles — the surface
that actually differs between docker and podman, and the one a real runtime
would only report on much later and much less legibly.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

import pytest

from tianluo.e2e.backend import BindMount, EnvironmentHandle, ServiceSpec
from tianluo.e2e.container_backend import (
    ContainerBackend,
    container_name,
    image_tag,
)
from tianluo.e2e.errors import E2EConfigError, E2EEnvironmentError

from ._stubs import FakeRunner, probe, sample_spec


def make_backend(runtime="docker", runner=None, **kwargs):
    runner = runner or FakeRunner()
    backend = ContainerBackend(probe(runtime), runner=runner, **kwargs)
    return backend, runner


def _app_runs(runner):
    """How many `run` attempts targeted the app service's container name."""
    return len(
        [a for a in runner.argv_for("run") if "tianluo-e2e-demo-app" in a]
    )


def started_handle(backend, runner, tmp_path, **spec_kwargs):
    spec = sample_spec(tmp_path, **spec_kwargs)
    handle = backend.create(spec)
    runner.calls.clear()
    backend.start(handle)
    return handle


class TestCreate:
    def test_creates_the_shared_network_first(self, tmp_path):
        backend, runner = make_backend()

        backend.create(sample_spec(tmp_path))

        assert runner.calls[0].argv[:3] == ["docker", "network", "create"]
        assert runner.calls[0].argv[3] == "tianluo-e2e-demo"

    def test_builds_services_with_a_template_and_pulls_the_rest(self, tmp_path):
        backend, runner = make_backend()

        handle = backend.create(sample_spec(tmp_path))

        build = runner.first("build")
        assert build is not None
        assert "-t" in build
        tag = build[build.index("-t") + 1]
        assert tag.startswith("tianluo-e2e/app:")
        # The external dependency service is used as published, not rebuilt.
        assert runner.first("pull") == ["docker", "pull", "postgres:16"]
        assert handle.images["db"] == "postgres:16"
        assert handle.images["app"] == tag

    def test_build_context_is_a_throwaway_dir_not_the_project_root(self, tmp_path):
        backend, runner = make_backend()

        backend.create(sample_spec(tmp_path))

        build = runner.first("build")
        context = Path(build[-1])
        assert context != tmp_path
        assert str(tmp_path) not in build

    def test_existing_network_is_reused_rather_than_recreated(self, tmp_path):
        runner = FakeRunner(
            [
                (
                    lambda a: a[1:3] == ["network", "create"],
                    (1, "", "network already exists"),
                )
            ]
        )
        backend, _ = make_backend(runner=runner)

        handle = backend.create(sample_spec(tmp_path))

        assert handle.network_id == "tianluo-e2e-demo"

    def test_network_failure_is_an_environment_error_with_remediation(self, tmp_path):
        runner = FakeRunner(
            [(lambda a: a[1:3] == ["network", "create"], (1, "", "permission denied"))]
        )
        backend, _ = make_backend(runner=runner)

        with pytest.raises(E2EEnvironmentError) as excinfo:
            backend.create(sample_spec(tmp_path))

        assert "permission denied" in str(excinfo.value)
        assert excinfo.value.remediation

    def test_build_failure_names_the_service(self, tmp_path):
        runner = FakeRunner([(lambda a: a[1] == "build", (1, "", "step 3 failed"))])
        backend, _ = make_backend(runner=runner)

        with pytest.raises(E2EEnvironmentError) as excinfo:
            backend.create(sample_spec(tmp_path))

        assert "app" in str(excinfo.value)

    def test_a_failed_build_leaves_the_created_network_recoverable(self, tmp_path):
        """The network is created first, so a failing build has already made one.

        `create` never returns in that case, so unless the handle it was filling
        in stays reachable the session cannot tear the network down and every
        failing run leaks one more of them.
        """
        runner = FakeRunner([(lambda a: a[1] == "build", (1, "", "step 3 failed"))])
        backend, _ = make_backend(runner=runner)

        with pytest.raises(E2EEnvironmentError):
            backend.create(sample_spec(tmp_path))

        partial = backend.last_handle
        assert partial is not None
        assert partial.network_id == "tianluo-e2e-demo"

        runner.calls.clear()
        backend.destroy(partial)
        assert ["docker", "network", "rm", "tianluo-e2e-demo"] in [
            call.argv for call in runner.calls
        ]

    def test_pull_failure_is_tolerated_when_the_image_is_already_local(self, tmp_path):
        runner = FakeRunner(
            [
                (lambda a: a[1] == "pull", (1, "", "no route to host")),
                (lambda a: a[1:3] == ["image", "inspect"], (0, "[{}]", "")),
            ]
        )
        backend, _ = make_backend(runner=runner)

        handle = backend.create(sample_spec(tmp_path))

        assert handle.images["db"] == "postgres:16"

    def test_pull_failure_without_a_local_image_raises(self, tmp_path):
        runner = FakeRunner(
            [
                (lambda a: a[1] == "pull", (1, "", "no route to host")),
                (lambda a: a[1:3] == ["image", "inspect"], (1, "", "no such image")),
            ]
        )
        backend, _ = make_backend(runner=runner)

        with pytest.raises(E2EEnvironmentError):
            backend.create(sample_spec(tmp_path))


class TestStart:
    def test_run_argv_wires_the_service_onto_the_shared_network(self, tmp_path):
        backend, runner = make_backend()
        started_handle(backend, runner, tmp_path)

        run = runner.first("run")

        assert run[:3] == ["docker", "run", "-d"]
        assert "--network" in run
        assert run[run.index("--network") + 1] == "tianluo-e2e-demo"
        # Peers address the service by its declared name regardless of the
        # collision-proof container name.
        assert run[run.index("--network-alias") + 1] == "app"
        assert run[run.index("--hostname") + 1] == "app"
        assert run[run.index("--name") + 1] == container_name("tianluo-e2e-demo", "app")

    def test_run_argv_carries_env_ports_workdir_and_image(self, tmp_path):
        backend, runner = make_backend()
        handle = started_handle(backend, runner, tmp_path)

        run = runner.first("run")

        assert "APP_ENV=test" in run
        assert "18000:8000" in run
        assert run[run.index("-w") + 1] == "/workspace"
        assert run[-1] == handle.images["app"]

    def test_source_is_bind_mounted_never_copied(self, tmp_path):
        backend, runner = make_backend()
        started_handle(backend, runner, tmp_path)

        run = runner.first("run")
        mount = run[run.index("-v") + 1]

        assert mount.startswith(tmp_path.as_posix() + ":/workspace")
        # And nothing in the image build referenced the source tree.
        assert not any("COPY" in a for a in runner.argv_for("build"))

    def test_selinux_label_is_applied_by_default_and_can_be_turned_off(self, tmp_path):
        backend, runner = make_backend()
        started_handle(backend, runner, tmp_path)
        labelled = runner.first("run")

        plain_backend, plain_runner = make_backend(selinux_label=False)
        started_handle(plain_backend, plain_runner, tmp_path)
        plain = plain_runner.first("run")

        assert labelled[labelled.index("-v") + 1].endswith(":Z")
        assert not plain[plain.index("-v") + 1].endswith(":Z")

    def test_a_source_mounted_into_two_services_gets_the_shared_label(
        self, tmp_path
    ):
        """`:Z` is private per container — the second start would revoke the first.

        On an SELinux host the app and the Playwright driver both bind-mount the
        project source; a private relabel by the browser container would take
        /workspace away from the app container mid-run, and the permission errors
        that follow look exactly like application bugs.
        """
        backend, runner = make_backend()
        services = (
            ServiceSpec(
                name="app",
                base_image="python:3.12-slim",
                mounts=(BindMount(source=tmp_path, target="/workspace"),),
            ),
            ServiceSpec(
                name="browser",
                base_image="mcr.microsoft.com/playwright:v1.44.0",
                mounts=(BindMount(source=tmp_path, target="/workspace"),),
            ),
        )
        started_handle(backend, runner, tmp_path, services=services)

        mounts = [
            argv[argv.index("-v") + 1]
            for argv in runner.argv_for("run")
            if "-v" in argv
        ]
        assert len(mounts) == 2
        assert all(mount.endswith(":z") for mount in mounts)

    def test_a_source_mounted_into_one_service_keeps_the_private_label(
        self, tmp_path
    ):
        backend, runner = make_backend()
        started_handle(backend, runner, tmp_path)

        run = runner.first("run")

        assert run[run.index("-v") + 1].endswith(":Z")

    def test_read_only_mount_renders_ro(self, tmp_path):
        backend, _ = make_backend()
        mount = BindMount(source=tmp_path, target="/fixtures", read_only=True)

        assert backend.format_mount(mount).endswith(":ro,Z")

    def test_podman_maps_the_host_uid_and_docker_does_not(self, tmp_path):
        docker, docker_runner = make_backend("docker")
        started_handle(docker, docker_runner, tmp_path)
        podman, podman_runner = make_backend("podman")
        started_handle(podman, podman_runner, tmp_path)

        docker_run = docker_runner.first("run")
        podman_run = podman_runner.first("run")

        assert "--userns=keep-id" in podman_run
        assert "--userns=keep-id" not in docker_run

    def test_a_podman_probe_invokes_the_podman_binary_for_every_verb(
        self, tmp_path
    ):
        """The binary switch itself, not just the flag deltas around it.

        Every other cross-runtime test here compares argv *after* argv[0] or keys
        on the probe's name, so a backend that ignored ``probe.binary`` and hard-
        coded ``docker`` would keep them all green — and then, on a podman-only
        host that had just passed preflight, every container operation would shell
        out to a binary that is not installed and report a host problem tianluo
        had itself manufactured.
        """
        backend, runner = make_backend("podman")
        handle = started_handle(backend, runner, tmp_path)
        backend.exec(handle, "app", ["true"])
        backend.destroy(handle)

        binaries = {call.argv[0] for call in runner.calls}

        assert binaries == {"podman"}
        # And the verbs really were exercised, so an empty call list cannot pass.
        assert {"run", "exec", "rm"} <= set(runner.verbs())

    def test_the_only_run_argv_difference_between_runtimes_is_uid_mapping(
        self, tmp_path
    ):
        """Both runtimes go through one verb wrapper; only the binary name and
        the rootless UID mapping may differ."""
        docker, docker_runner = make_backend("docker")
        started_handle(docker, docker_runner, tmp_path)
        podman, podman_runner = make_backend("podman")
        started_handle(podman, podman_runner, tmp_path)

        docker_run = docker_runner.first("run")[1:]
        podman_run = podman_runner.first("run")[1:]

        assert [a for a in podman_run if a != "--userns=keep-id"] == docker_run

    def test_oci_runtime_is_passed_through_only_when_configured(self, tmp_path):
        with_kata, kata_runner = make_backend(oci_runtime="kata-runtime")
        started_handle(with_kata, kata_runner, tmp_path)
        without, plain_runner = make_backend()
        started_handle(without, plain_runner, tmp_path)

        kata_run = kata_runner.first("run")
        plain_run = plain_runner.first("run")

        assert kata_run[kata_run.index("--runtime") + 1] == "kata-runtime"
        assert "--runtime" not in plain_run

    def test_container_ids_are_recorded_and_the_handle_is_marked_started(
        self, tmp_path
    ):
        runner = FakeRunner([(lambda a: a[1] == "run", (0, "cid-123\n", ""))])
        backend, _ = make_backend(runner=runner)

        handle = started_handle(backend, runner, tmp_path)

        assert handle.containers["app"] == "cid-123"
        assert handle.started is True

    def test_run_failure_is_an_environment_error(self, tmp_path):
        runner = FakeRunner([(lambda a: a[1] == "run", (1, "", "port already in use"))])
        backend, _ = make_backend(runner=runner)
        spec = sample_spec(tmp_path)
        handle = backend.create(spec)

        with pytest.raises(E2EEnvironmentError) as excinfo:
            backend.start(handle)

        assert "app" in str(excinfo.value)

    def test_a_leftover_container_under_our_own_name_is_replaced(self, tmp_path):
        """`keep_environment` plus a deterministic name would wedge the fix loop.

        The container name is `<network>-<service>` and the network carries the
        flow id, so the next iteration of the same flow asks for exactly the name
        the kept environment still holds. Failing there reports a host-permission
        remediation for a host that is fine, and stops a bounded fix loop dead.
        """
        attempts = {"n": 0}

        def run_result(argv):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return (
                    1,
                    "",
                    'Error response from daemon: Conflict. The container name '
                    '"/tianluo-e2e-demo-app" is already in use by container "old".',
                )
            return (0, "cid-new\n", "")

        runner = FakeRunner([(lambda a: a[1] == "run", run_result)])
        backend, _ = make_backend(runner=runner)
        handle = backend.create(sample_spec(tmp_path))
        runner.calls.clear()

        backend.start(handle)

        assert ["docker", "rm", "-f", "-v", "tianluo-e2e-demo-app"] in [
            call.argv for call in runner.calls
        ]
        assert _app_runs(runner) == 2
        assert handle.containers["app"] == "cid-new"

    def test_podman_name_conflict_wording_is_recognised_too(self, tmp_path):
        attempts = {"n": 0}

        def run_result(argv):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return (
                    1,
                    "",
                    'Error: creating container storage: the container name '
                    '"tianluo-e2e-demo-app" is already in use by 9f2c.',
                )
            return (0, "cid-new\n", "")

        runner = FakeRunner([(lambda a: a[1] == "run", run_result)])
        backend, _ = make_backend("podman", runner=runner)
        handle = backend.create(sample_spec(tmp_path))

        backend.start(handle)

        assert _app_runs(runner) == 2
        assert handle.containers["app"] == "cid-new"

    def test_a_port_conflict_is_not_mistaken_for_a_name_conflict(self, tmp_path):
        """A busy host port belongs to a foreign process — removing our container
        cannot free it, and retrying would only hide the real cause."""
        runner = FakeRunner(
            [
                (
                    lambda a: a[1] == "run",
                    (1, "", "Bind for 0.0.0.0:18000 failed: port is already allocated"),
                )
            ]
        )
        backend, _ = make_backend(runner=runner)
        handle = backend.create(sample_spec(tmp_path))
        runner.calls.clear()

        with pytest.raises(E2EEnvironmentError):
            backend.start(handle)

        assert _app_runs(runner) == 1
        assert runner.argv_for("rm") == []

    def test_a_persistent_name_conflict_still_reports_the_service(self, tmp_path):
        runner = FakeRunner(
            [
                (
                    lambda a: a[1] == "run",
                    (1, "", 'the container name "x" is already in use'),
                ),
                (lambda a: a[1] == "rm", (1, "", "permission denied")),
            ]
        )
        backend, _ = make_backend(runner=runner)
        handle = backend.create(sample_spec(tmp_path))

        with pytest.raises(E2EEnvironmentError) as excinfo:
            backend.start(handle)

        assert "app" in str(excinfo.value)
        assert _app_runs(runner) == 2

    def test_buildkit_is_requested_for_docker_but_not_podman(self, tmp_path):
        docker, docker_runner = make_backend("docker")
        docker.create(sample_spec(tmp_path))
        podman, podman_runner = make_backend("podman")
        podman.create(sample_spec(tmp_path))

        docker_env = docker_runner.calls[0].kwargs["env"]
        podman_env = podman_runner.calls[0].kwargs["env"]

        assert docker_env["DOCKER_BUILDKIT"] == "1"
        assert "DOCKER_BUILDKIT" not in podman_env


class TestExec:
    def test_exec_targets_the_service_container(self, tmp_path):
        backend, runner = make_backend()
        handle = started_handle(backend, runner, tmp_path)
        runner.calls.clear()

        result = backend.exec(handle, "app", ["pytest", "-q"], workdir="/workspace")

        argv = runner.first("exec")
        assert argv[:2] == ["docker", "exec"]
        assert argv[-2:] == ["pytest", "-q"]
        assert argv[argv.index("-w") + 1] == "/workspace"
        assert result.ok is True

    def test_exec_forwards_environment(self, tmp_path):
        backend, runner = make_backend()
        handle = started_handle(backend, runner, tmp_path)

        backend.exec(handle, "app", ["true"], environment={"CI": "1"})

        assert "CI=1" in runner.first("exec")

    def test_exec_on_an_undeclared_service_is_a_config_error(self, tmp_path):
        backend, runner = make_backend()
        handle = started_handle(backend, runner, tmp_path)

        with pytest.raises(E2EConfigError):
            backend.exec(handle, "nope", ["true"])

    def test_exec_before_start_is_an_environment_error(self, tmp_path):
        backend, runner = make_backend()
        handle = backend.create(sample_spec(tmp_path))

        with pytest.raises(E2EEnvironmentError):
            backend.exec(handle, "app", ["true"])

    def test_timeout_is_reported_not_raised(self, tmp_path):
        runner = FakeRunner()
        backend, _ = make_backend(runner=runner)
        handle = started_handle(backend, runner, tmp_path)
        runner.respond(
            lambda a: a[1] == "exec", subprocess.TimeoutExpired(cmd="docker", timeout=1)
        )

        result = backend.exec(handle, "app", ["sleep", "100"], timeout=1)

        assert result.timed_out is True
        assert result.ok is False

    def test_a_vanished_binary_becomes_an_environment_error(self, tmp_path):
        runner = FakeRunner([(lambda a: True, FileNotFoundError("docker"))])
        backend, _ = make_backend(runner=runner)

        with pytest.raises(E2EEnvironmentError) as excinfo:
            backend.create(sample_spec(tmp_path))

        assert excinfo.value.remediation


class TestSnapshot:
    def test_file_snapshot_copies_out_of_the_container(self, tmp_path):
        backend, runner = make_backend()
        handle = started_handle(backend, runner, tmp_path)
        destination = tmp_path / "out" / "report.json"

        snapshot = backend.snapshot(
            handle, "app", "/workspace/report.json", destination=destination
        )

        argv = runner.first("cp")
        container = container_name("tianluo-e2e-demo", "app")
        assert argv[2].endswith(":/workspace/report.json")
        assert argv[3] == str(destination)
        assert snapshot.path == destination
        assert snapshot.kind == "file"
        assert container in argv[2] or handle.containers["app"] in argv[2]

    def test_screenshot_captures_with_scrot_then_copies(self, tmp_path):
        backend, runner = make_backend()
        handle = started_handle(backend, runner, tmp_path)
        runner.calls.clear()

        snapshot = backend.snapshot(
            handle,
            "app",
            "/tmp/shot.png",
            kind="screenshot",
            destination=tmp_path / "shot.png",
        )

        exec_argv = runner.first("exec")
        assert exec_argv[-3:] == ["scrot", "-o", "/tmp/shot.png"]
        assert runner.first("cp") is not None
        assert snapshot.kind == "screenshot"

    def test_screenshot_failure_is_an_environment_error(self, tmp_path):
        runner = FakeRunner()
        backend, _ = make_backend(runner=runner)
        handle = started_handle(backend, runner, tmp_path)
        runner.respond(lambda a: a[1] == "exec", (1, "", "no display"))

        with pytest.raises(E2EEnvironmentError):
            backend.snapshot(handle, "app", "/tmp/s.png", kind="screenshot")

    def test_log_snapshot_exposes_the_text_in_metadata(self, tmp_path):
        runner = FakeRunner(
            [(lambda a: a[1] == "logs", (0, "listening on 8000\n", ""))]
        )
        backend, _ = make_backend(runner=runner)
        handle = started_handle(backend, runner, tmp_path)

        snapshot = backend.snapshot(
            handle, "app", "", kind="log", destination=tmp_path / "app.log"
        )

        assert "listening on 8000" in snapshot.metadata["text"]
        assert "listening on 8000" in snapshot.path.read_text(encoding="utf-8")

    def test_log_snapshot_without_a_destination_writes_no_host_file(self, tmp_path):
        """A `log` readiness probe snapshots once per poll.

        Persisting a temp file per poll leaks one file per attempt — dozens per
        service start — for text the caller reads straight out of the metadata.
        """
        runner = FakeRunner([(lambda a: a[1] == "logs", (0, "booting\n", ""))])
        backend, _ = make_backend(runner=runner)
        handle = started_handle(backend, runner, tmp_path)

        before = sorted(Path(tempfile.gettempdir()).glob("tianluo-e2e-*.log"))
        snapshot = backend.snapshot(handle, "app", "", kind="log")
        after = sorted(Path(tempfile.gettempdir()).glob("tianluo-e2e-*.log"))

        assert snapshot.path is None
        assert snapshot.metadata["text"] == "booting\n"
        assert after == before

    def test_file_copy_failure_is_an_environment_error(self, tmp_path):
        runner = FakeRunner([(lambda a: a[1] == "cp", (1, "", "no such file"))])
        backend, _ = make_backend(runner=runner)
        handle = started_handle(backend, runner, tmp_path)

        with pytest.raises(E2EEnvironmentError):
            backend.snapshot(handle, "app", "/nope")

    def test_a_failed_copy_leaves_no_temp_file_behind(self, tmp_path):
        """One empty file per failed extraction would fill the host's /tmp."""
        runner = FakeRunner([(lambda a: a[1] == "cp", (1, "", "no such file"))])
        backend, _ = make_backend(runner=runner)
        handle = started_handle(backend, runner, tmp_path)

        with pytest.raises(E2EEnvironmentError):
            backend.snapshot(handle, "app", "/nope")

        leftover = Path(runner.first("cp")[3])
        assert not leftover.exists()

    def test_unknown_snapshot_kind_is_a_config_error(self, tmp_path):
        backend, runner = make_backend()
        handle = started_handle(backend, runner, tmp_path)

        with pytest.raises(E2EConfigError):
            backend.snapshot(handle, "app", "/x", kind="hologram")


class TestDestroy:
    def test_removes_every_container_and_the_network(self, tmp_path):
        backend, runner = make_backend()
        handle = started_handle(backend, runner, tmp_path)
        runner.calls.clear()

        backend.destroy(handle)

        removed = [a for a in runner.argv_for("rm")]
        assert len(removed) == 2
        assert runner.argv_for("network")[-1][1:3] == ["network", "rm"]
        assert handle.containers == {}
        assert handle.network_id is None
        assert handle.started is False

    def test_is_idempotent(self, tmp_path):
        runner = FakeRunner()
        backend, _ = make_backend(runner=runner)
        handle = started_handle(backend, runner, tmp_path)
        runner.respond(
            lambda a: a[1] == "rm" or a[1:3] == ["network", "rm"],
            (1, "", "Error: no such container"),
        )

        backend.destroy(handle)
        backend.destroy(handle)  # must not raise on an already-torn-down handle

        assert handle.containers == {}

    def test_removes_by_deterministic_name_when_the_run_never_recorded_one(
        self, tmp_path
    ):
        """A stale container must not be able to wedge the environment forever.

        Container names are deterministic per flow, so a run killed between
        `start` and `destroy` — or a `run` that fails on "name already in use" —
        leaves a container nothing recorded. Teardown driven by the recorded map
        alone would then never remove it, the network would keep active
        endpoints, and every later iteration would fail identically.
        """
        runner = FakeRunner([(lambda a: a[1] == "run", (1, "", "name already in use"))])
        backend, _ = make_backend(runner=runner)
        handle = backend.create(sample_spec(tmp_path))

        with pytest.raises(E2EEnvironmentError):
            backend.start(handle)

        assert handle.containers == {}
        runner.calls.clear()
        backend.destroy(handle)

        removed = [argv[-1] for argv in runner.argv_for("rm")]
        assert removed == [
            container_name("tianluo-e2e-demo", "app"),
            container_name("tianluo-e2e-demo", "db"),
        ]

    def test_survives_a_half_finished_create(self, tmp_path):
        backend, runner = make_backend()
        handle = EnvironmentHandle(runtime="docker", spec=sample_spec(tmp_path))

        backend.destroy(handle)

        assert handle.started is False

    def test_unexpected_removal_failure_is_logged_not_raised(self, tmp_path, caplog):
        runner = FakeRunner()
        backend, _ = make_backend(runner=runner)
        handle = started_handle(backend, runner, tmp_path)
        runner.respond(lambda a: a[1] == "rm", (1, "", "device or resource busy"))

        with caplog.at_level(logging.WARNING, logger="tianluo.e2e.container_backend"):
            backend.destroy(handle)  # teardown must never mask the real failure

        assert any(
            "could not remove e2e container" in record.getMessage()
            for record in caplog.records
        )


class TestJsonParsing:
    @pytest.mark.parametrize("text", ["", "   ", "\n"])
    def test_empty_output_parses_to_none(self, text):
        assert ContainerBackend.parse_json(text) is None

    def test_malformed_output_parses_to_none(self):
        assert ContainerBackend.parse_json("{not json at all") is None

    def test_well_formed_output_parses(self):
        assert ContainerBackend.parse_json('{"Id": "abc"}') == {"Id": "abc"}

    def test_line_delimited_output_falls_back_to_the_first_document(self):
        text = 'WARNING: something\n{"Id": "abc"}\n{"Id": "def"}\n'

        assert ContainerBackend.parse_json(text) == {"Id": "abc"}

    def test_inspect_uses_the_portable_go_template_form(self, tmp_path):
        runner = FakeRunner([(lambda a: a[1] == "inspect", (0, '{"Id": "abc"}', ""))])
        backend, _ = make_backend(runner=runner)

        assert backend.inspect("some-container") == {"Id": "abc"}
        assert "{{json .}}" in runner.first("inspect")

    def test_inspect_of_a_missing_object_returns_none(self):
        runner = FakeRunner([(lambda a: a[1] == "inspect", (1, "", "no such object"))])
        backend, _ = make_backend(runner=runner)

        assert backend.inspect("ghost") is None


class TestNaming:
    def test_container_names_are_namespaced_by_network(self):
        assert container_name("tianluo-e2e-demo", "app") == "tianluo-e2e-demo-app"

    def test_unsafe_characters_are_sanitized(self):
        assert container_name("proj/one", "web app") == "proj-one-web-app"

    def test_image_tags_are_namespaced(self):
        assert image_tag("app", "abc123") == "tianluo-e2e/app:abc123"

    def test_image_tag_is_keyed_to_the_build_recipe_not_the_flow(self, tmp_path):
        """An unchanged environment must resolve to the tag the last flow built.

        The network name carries the flow id, so a network-derived tag minted one
        orphaned image per flow that no later flow could reuse.
        """
        first, first_runner = make_backend()
        second, second_runner = make_backend()

        one = first.create(sample_spec(tmp_path, network="tianluo-e2e-flow-a"))
        two = second.create(sample_spec(tmp_path, network="tianluo-e2e-flow-b"))

        assert one.images["app"] == two.images["app"]
        assert "flow-a" not in one.images["app"]

    def test_a_changed_build_recipe_gets_its_own_tag(self, tmp_path):
        backend, _ = make_backend()

        one = backend.create(sample_spec(tmp_path))
        two = backend.create(
            sample_spec(
                tmp_path,
                services=(
                    ServiceSpec(
                        name="app",
                        base_image="python:3.12-slim",
                        template="base",
                        build_steps=("pip install -e .", "pip install pytest"),
                    ),
                ),
            )
        )

        assert one.images["app"] != two.images["app"]


def test_no_direct_subprocess_run_call_outside_the_injection_point():
    """Every runtime invocation must flow through the injected ``runner``."""
    from tianluo.e2e import container_backend as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    occurrences = [
        line
        for line in source.splitlines()
        if "subprocess.run" in line and "runner: Runner = subprocess.run" not in line
    ]
    assert occurrences == []
