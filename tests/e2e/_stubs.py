"""Shared stubs for the e2e backend tests.

Every test in this package runs without docker or podman installed: the backend
takes its subprocess entry point as an injected ``runner``, so a recorded call
list plus canned results is enough to pin the exact argv each verb assembles —
which is the part that actually differs between the two runtimes.
"""

from __future__ import annotations

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
    """Minimal backend for testing layers above the container implementation."""

    def __init__(
        self,
        *,
        exec_results: Optional[Sequence[ExecResult]] = None,
        log_text: str = "",
    ) -> None:
        self.exec_results = list(exec_results or [])
        self.log_text = log_text
        self.exec_calls: List[Any] = []
        self.snapshot_calls: List[Any] = []
        self.destroyed = 0

    def create(self, spec):
        return EnvironmentHandle(runtime="fake", spec=spec)

    def start(self, handle):
        handle.started = True

    def exec(self, handle, service, argv, **kwargs):
        self.exec_calls.append((service, list(argv), kwargs))
        if self.exec_results:
            return self.exec_results.pop(0)
        return ExecResult(exit_code=0)

    def snapshot(self, handle, service, target, *, kind="file", destination=None):
        self.snapshot_calls.append((service, target, kind))
        return Snapshot(
            kind=kind,
            path=Path(destination or "/tmp/tianluo-e2e-fake.log"),
            service=service,
            metadata={"text": self.log_text},
        )

    def destroy(self, handle):
        self.destroyed += 1


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
