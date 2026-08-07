"""Shared stubs for the e2e backend tests.

Every test in this package runs without docker or podman installed: the backend
takes its subprocess entry point as an injected ``runner``, so a recorded call
list plus canned results is enough to pin the exact argv each verb assembles —
which is the part that actually differs between the two runtimes.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from tianluo.e2e.backend import (
    BindMount,
    EnvironmentHandle,
    EnvironmentSpec,
    ExecResult,
    IsolationBackend,
    ServiceSpec,
    Snapshot,
)
from tianluo.e2e.runtime_probe import RuntimeProbeResult


class Call:
    """One recorded runtime CLI invocation."""

    def __init__(self, argv: Sequence[str], kwargs: Dict[str, Any]) -> None:
        self.argv = list(argv)
        self.kwargs = dict(kwargs)

    @property
    def binary(self) -> str:
        return self.argv[0]

    @property
    def verb(self) -> str:
        return self.argv[1] if len(self.argv) > 1 else ""

    @property
    def args(self) -> List[str]:
        return self.argv[1:]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Call({!r})".format(self.argv)


class FakeRunner:
    """Records every invocation and answers from a rule list.

    A rule is ``(predicate, result)`` where ``predicate`` receives the argv and
    ``result`` is a ``(returncode, stdout, stderr)`` triple, an exception to
    raise, or a callable taking the argv. The first matching rule wins;
    unmatched calls succeed with empty output.
    """

    def __init__(self, rules: Optional[Sequence[Any]] = None) -> None:
        self.calls: List[Call] = []
        self.rules = list(rules or [])

    def respond(
        self, predicate: Callable[[List[str]], bool], result: Any
    ) -> "FakeRunner":
        self.rules.append((predicate, result))
        return self

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(Call(argv, kwargs))
        for predicate, result in self.rules:
            if predicate(argv):
                if isinstance(result, BaseException):
                    raise result
                if callable(result):
                    result = result(argv)
                code, out, err = result
                return subprocess.CompletedProcess(argv, code, out, err)
        return subprocess.CompletedProcess(argv, 0, "", "")

    # -- convenience accessors -------------------------------------------

    def argv_for(self, verb: str) -> List[List[str]]:
        return [c.argv for c in self.calls if c.verb == verb]

    def first(self, verb: str) -> Optional[List[str]]:
        matches = self.argv_for(verb)
        return matches[0] if matches else None

    def verbs(self) -> List[str]:
        return [c.verb for c in self.calls]


def probe(name: str = "docker", *, rootless: bool = False) -> RuntimeProbeResult:
    return RuntimeProbeResult(name=name, binary=name, ok=True, rootless=rootless)


def sample_spec(
    project_root: Path,
    *,
    network: str = "tianluo-e2e-demo",
    services: Optional[Sequence[ServiceSpec]] = None,
) -> EnvironmentSpec:
    if services is None:
        services = (
            ServiceSpec(
                name="app",
                base_image="python:3.12-slim",
                template="base",
                build_steps=("pip install -e .",),
                mounts=(BindMount(source=project_root, target="/workspace"),),
                environment={"APP_ENV": "test"},
                ports=("18000:8000",),
                workdir="/workspace",
            ),
            ServiceSpec(name="db", base_image="postgres:16"),
        )
    return EnvironmentSpec(
        project_root=project_root, network=network, services=tuple(services)
    )


class FakeBackend(IsolationBackend):
    """In-memory :class:`IsolationBackend` shared by every test above the
    container implementation (readiness, assertions, executor, session).

    Three ways to script ``exec``, in precedence order: ``exec_handler`` (a
    callable receiving ``(service, argv)``), ``exec_results`` (a queue popped in
    order), and otherwise a plain success. ``exec_error`` makes the next call
    raise, which is how the session's teardown-on-exception path is exercised.
    """

    def __init__(
        self,
        *,
        exec_results: Optional[Sequence[ExecResult]] = None,
        exec_handler: Optional[Callable[[str, List[str]], ExecResult]] = None,
        exec_error: Optional[BaseException] = None,
        create_error: Optional[BaseException] = None,
        start_error: Optional[BaseException] = None,
        snapshot_error: Optional[BaseException] = None,
        log_text: str = "",
        screenshot_bytes: Optional[bytes] = None,
    ) -> None:
        self.exec_results = list(exec_results or [])
        self.exec_handler = exec_handler
        self.exec_error = exec_error
        self.create_error = create_error
        self.start_error = start_error
        self.snapshot_error = snapshot_error
        self.log_text = log_text
        self.screenshot_bytes = screenshot_bytes
        self.exec_calls: List[Any] = []
        self.snapshot_calls: List[Any] = []
        self.created: List[EnvironmentSpec] = []
        self.started: List[EnvironmentHandle] = []
        self.destroyed = 0
        self.handle: Optional[EnvironmentHandle] = None

    def create(self, spec):
        if self.create_error is not None:
            raise self.create_error
        self.created.append(spec)
        handle = EnvironmentHandle(runtime="fake", spec=spec)
        # Containers are recorded so `destroy` has something to report and the
        # keep-environment hint has names to print, mirroring the real backend.
        for service in spec.services:
            handle.containers[service.name] = "fake-{}".format(service.name)
        self.handle = handle
        return handle

    def start(self, handle):
        if self.start_error is not None:
            raise self.start_error
        self.started.append(handle)
        handle.started = True

    def exec(self, handle, service, argv, **kwargs):
        argv = [str(part) for part in argv]
        self.exec_calls.append((service, argv, kwargs))
        if self.exec_error is not None:
            raise self.exec_error
        if self.exec_handler is not None:
            return self.exec_handler(service, argv)
        if self.exec_results:
            return self.exec_results.pop(0)
        return ExecResult(exit_code=0)

    def snapshot(self, handle, service, target, *, kind="file", destination=None):
        self.snapshot_calls.append((service, target, kind))
        if self.snapshot_error is not None:
            raise self.snapshot_error
        path = Path(destination) if destination else Path("/tmp/tianluo-e2e-fake.log")
        if destination is not None and self.screenshot_bytes is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.screenshot_bytes)
        return Snapshot(
            kind=kind,
            path=path,
            service=service,
            metadata={"text": self.log_text},
        )

    def destroy(self, handle):
        self.destroyed += 1

    # -- convenience accessors -------------------------------------------

    def argv_containing(self, needle: str) -> List[List[str]]:
        """Every recorded argv containing ``needle`` as one of its parts."""
        return [argv for _, argv, _ in self.exec_calls if needle in argv]


def marked(payload: Dict[str, Any]) -> str:
    """stdout as an in-container helper program writes it (marker + JSON)."""
    from tianluo.e2e.assertions import RESULT_MARKER

    return "some unrelated container noise\n{} {}\n".format(
        RESULT_MARKER, json.dumps(payload)
    )


def assertion(kind: str, **params: Any):
    """Build an :class:`AssertionDecl`, splitting out the tier-declaration flags."""
    from tianluo.e2e.content_config import AssertionDecl

    flags = {
        name: bool(params.pop(name, False))
        for name in ("visual_regression", "semantic_visual", "require_evidence")
    }
    return AssertionDecl(kind=kind, params=params, **flags)


def action(kind: str, **params: Any):
    """Build an :class:`ActionDecl`."""
    from tianluo.e2e.content_config import ActionDecl

    return ActionDecl(kind=kind, params=params)


def scenario(
    name: str = "smoke",
    driver: str = "app",
    *,
    actions: Sequence[Any] = (),
    assertions: Sequence[Any] = (),
    source: str = "tianluo/e2e/scenarios/smoke.yaml",
    **kwargs: Any,
):
    """Build a :class:`ScenarioDecl` without going through YAML."""
    from tianluo.e2e.content_config import ScenarioDecl

    return ScenarioDecl(
        name=name,
        driver=driver,
        source=source,
        actions=tuple(actions),
        assertions=tuple(assertions),
        **kwargs,
    )


def service_decl(name: str = "app", **kwargs: Any):
    """Build a :class:`ServiceDecl` with a sane default image."""
    from tianluo.e2e.content_config import ServiceDecl

    kwargs.setdefault("image", "python:3.12-slim")
    return ServiceDecl(name=name, **kwargs)


def content(
    project_root: Path,
    *,
    services: Optional[Sequence[Any]] = None,
    scenarios: Optional[Sequence[Any]] = None,
    network: str = "tianluo-e2e",
):
    """Build an :class:`E2EContent` directly, bypassing the YAML layer."""
    from tianluo.e2e.content_config import E2EContent

    root = Path(project_root)
    return E2EContent(
        project_root=root,
        root=root / "tianluo" / "e2e",
        network=network,
        services=tuple(services if services is not None else (service_decl(),)),
        scenarios=tuple(scenarios or ()),
        baselines=root / "tianluo" / "e2e" / "baselines",
    )


def write_png(path: Path, *, size=(4, 4), color=(10, 20, 30), pixels=None) -> Path:
    """Write a tiny PNG for the tier-2 comparison tests."""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", size, color)
    for coordinate, value in (pixels or {}).items():
        image.putpixel(coordinate, value)
    image.save(path, format="PNG")
    return path


class FakeClock:
    """Monotonic clock that only advances when the code under test sleeps."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.slept: List[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += max(seconds, 0.001)
