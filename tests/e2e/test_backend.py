"""Tests for ``tianluo.e2e.backend`` — the isolation-backend contract.

The interface is deliberately narrow so a future VM backend needs no change
above this line; these tests pin that narrowness (exactly five verbs, all
abstract) and the shape of the data contracts the layers above consume.
"""

from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path

import pytest

from tianluo.e2e import backend as backend_mod
from tianluo.e2e.backend import (
    BindMount,
    EnvironmentHandle,
    EnvironmentSpec,
    ExecResult,
    IsolationBackend,
    ReadinessProbe,
    ServiceSpec,
    Snapshot,
)

EXPECTED_VERBS = {"create", "start", "exec", "snapshot", "destroy"}


class TestIsolationBackendInterface:
    def test_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            IsolationBackend()  # type: ignore[abstract]

    def test_exposes_exactly_the_five_narrow_verbs(self):
        assert IsolationBackend.__abstractmethods__ == frozenset(EXPECTED_VERBS)

    def test_declares_no_extra_public_methods(self):
        public = {
            name
            for name, _ in inspect.getmembers(IsolationBackend, inspect.isfunction)
            if not name.startswith("_")
        }
        assert public == EXPECTED_VERBS

    def test_a_partial_implementation_is_still_abstract(self):
        class Partial(IsolationBackend):
            def create(self, spec):  # pragma: no cover - never instantiated
                raise NotImplementedError

        with pytest.raises(TypeError):
            Partial()  # type: ignore[abstract]

    def test_a_complete_implementation_instantiates(self):
        class Fake(IsolationBackend):
            def create(self, spec):
                return EnvironmentHandle(runtime="docker", spec=spec)

            def start(self, handle):
                handle.started = True

            def exec(self, handle, service, argv, **kwargs):
                return ExecResult(exit_code=0, stdout="hi")

            def snapshot(self, handle, service, target, **kwargs):
                return Snapshot(kind="file", path=Path(target), service=service)

            def destroy(self, handle):
                handle.containers.clear()

        spec = EnvironmentSpec(project_root=Path("/tmp/p"), network="net")
        fake = Fake()
        handle = fake.create(spec)
        fake.start(handle)

        assert handle.started is True
        assert fake.exec(handle, "app", ["true"]).ok is True


class TestDataContracts:
    def test_service_spec_defaults_describe_a_pull_only_service(self):
        svc = ServiceSpec(name="db", base_image="postgres:16")

        # No template and no build steps: the image is used as published, which
        # is how external dependency services are declared.
        assert svc.template is None
        assert svc.build_steps == ()
        assert svc.readiness is None
        assert dict(svc.environment) == {}

    def test_environment_spec_looks_services_up_by_name(self):
        app = ServiceSpec(name="app", base_image="python:3.12-slim")
        db = ServiceSpec(name="db", base_image="postgres:16")
        spec = EnvironmentSpec(
            project_root=Path("/tmp/p"), network="net", services=(app, db)
        )

        assert spec.service("db") is db
        assert spec.service("missing") is None

    def test_specs_are_immutable(self):
        svc = ServiceSpec(name="app", base_image="python:3.12-slim")

        with pytest.raises(dataclasses.FrozenInstanceError):
            svc.name = "other"  # type: ignore[misc]

    def test_handle_is_mutable_so_partial_creation_is_still_destroyable(self):
        spec = EnvironmentSpec(project_root=Path("/tmp/p"), network="net")
        handle = EnvironmentHandle(runtime="podman", spec=spec)

        handle.network_id = "net-1"
        handle.containers["app"] = "cid-1"

        assert handle.network_id == "net-1"
        assert handle.containers == {"app": "cid-1"}
        assert handle.started is False

    def test_exec_result_ok_requires_zero_exit_and_no_timeout(self):
        assert ExecResult(exit_code=0).ok is True
        assert ExecResult(exit_code=1).ok is False
        assert ExecResult(exit_code=0, timed_out=True).ok is False

    def test_bind_mount_is_read_write_by_default(self):
        mount = BindMount(source=Path("/tmp/p"), target="/workspace")

        assert mount.read_only is False

    def test_readiness_probe_carries_a_budget(self):
        probe = ReadinessProbe(kind="http", url="http://app:8000/healthz")

        assert probe.timeout > 0
        assert probe.interval > 0


def test_module_imports_only_stdlib():
    """No third-party import may creep into the backend contract module."""
    source = Path(backend_mod.__file__).read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")) and " import " in f" {stripped} ":
            top = stripped.split()[1].split(".")[0]
            assert top in {
                "abc",
                "dataclasses",
                "pathlib",
                "typing",
                "__future__",
            }, f"non-stdlib import in backend.py: {stripped}"
