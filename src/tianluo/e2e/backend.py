"""tianluo.e2e.backend — the isolation-backend abstraction and its data contracts.

An e2e environment is *a group of containers on a shared network*: each service
picks its own base image and build layer, tianluo wires them together so they
reach each other by service name, and scenarios run inside whichever service is
declared the driver (a Playwright container for browser scenarios, the
application container itself for pure-CLI ones). A single-container environment
is just the degenerate case of that topology.

This module defines *what a backend must do*, not *how*: the container backend
(``docker``/``podman`` via a thin subprocess wrapper) is the first and currently
only implementation. Everything here is stdlib-only and carries no third-party
import, so it stays importable on a core-only install.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

__all__ = [
    "BindMount",
    "EnvironmentHandle",
    "EnvironmentSpec",
    "ExecResult",
    "IsolationBackend",
    "ReadinessProbe",
    "ServiceSpec",
    "Snapshot",
]


@dataclass(frozen=True)
class BindMount:
    """A host path exposed inside a service container.

    WHY: the project's source tree is bind-mounted rather than ``COPY``-ed into
    the image. Every fix-loop iteration edits the source, and baking it into the
    image would invalidate the ``COPY`` layer and everything after it on every
    single round, turning a seconds-long re-run into a minutes-long rebuild.
    With a bind mount the image is rebuilt only when the *environment build*
    configuration itself changes; an iteration just restarts the container.
    """

    source: Path
    target: str
    read_only: bool = False


@dataclass(frozen=True)
class ReadinessProbe:
    """How to tell that a service has finished starting.

    ``kind`` selects the mechanism — ``"command"`` (run ``command`` in the
    container until it exits 0), ``"http"`` (GET ``url`` until it answers with
    an acceptable status), ``"tcp"`` (connect to ``port``), ``"log"`` (wait for
    ``pattern`` in the container log). A service with no probe is considered
    ready as soon as its container is running.
    """

    kind: str
    command: Tuple[str, ...] = ()
    url: Optional[str] = None
    port: Optional[int] = None
    pattern: Optional[str] = None
    # Total budget for this probe, and the pause between attempts.
    timeout: float = 60.0
    interval: float = 1.0


@dataclass(frozen=True)
class ServiceSpec:
    """One container in the environment topology.

    ``base_image`` is the source of the base layer — a public registry image
    (``python:3.12-slim``, ``postgres:16``, a Playwright official image) or the
    base a tianluo Dockerfile template builds on. When ``template`` is ``None``
    *and* ``build_steps`` is empty the image is pulled and used as-is (the
    external-dependency case: databases, caches); otherwise a Dockerfile is
    rendered from ``template`` with ``build_steps`` expanded into layers and
    built locally.

    ``name`` doubles as the container name and the DNS name peers use to reach
    this service on the shared network.
    """

    name: str
    base_image: str
    template: Optional[str] = None
    build_steps: Tuple[str, ...] = ()
    readiness: Optional[ReadinessProbe] = None
    ports: Tuple[str, ...] = ()
    environment: Mapping[str, str] = field(default_factory=dict)
    mounts: Tuple[BindMount, ...] = ()
    command: Tuple[str, ...] = ()
    workdir: Optional[str] = None


@dataclass(frozen=True)
class EnvironmentSpec:
    """A whole e2e environment: the services plus the network joining them."""

    project_root: Path
    network: str
    services: Tuple[ServiceSpec, ...] = ()
    labels: Mapping[str, str] = field(default_factory=dict)

    def service(self, name: str) -> Optional[ServiceSpec]:
        """Return the service declared under ``name``, or ``None``."""
        for svc in self.services:
            if svc.name == name:
                return svc
        return None


@dataclass
class EnvironmentHandle:
    """The live counterpart of an :class:`EnvironmentSpec`.

    Mutable on purpose: :meth:`IsolationBackend.create` and
    :meth:`IsolationBackend.start` fill ``network_id`` / ``containers`` /
    ``images`` in as resources come up, and :meth:`IsolationBackend.destroy`
    needs whatever was created so far even when creation aborted halfway.
    """

    runtime: str
    spec: EnvironmentSpec
    network_id: Optional[str] = None
    # service name -> container id
    containers: Dict[str, str] = field(default_factory=dict)
    # service name -> image reference actually used
    images: Dict[str, str] = field(default_factory=dict)
    started: bool = False


@dataclass(frozen=True)
class ExecResult:
    """Outcome of one command executed inside a service container."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True)
class Snapshot:
    """An artifact pulled out of a running environment.

    ``kind`` says what it is (``"screenshot"``, ``"file"``, ``"log"``);
    ``path`` is where it landed on the host, which is what the assertion layer
    compares against a git-tracked baseline.
    """

    kind: str
    path: Path
    service: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


class IsolationBackend(ABC):
    """Pluggable execution backend for one e2e environment.

    WHY: the interface is kept to five verbs on purpose. ``create`` / ``start``
    / ``exec`` / ``snapshot`` / ``destroy`` is the smallest set that expresses
    everything the executor and the session orchestrator above it need, and
    every one of them is equally meaningful for a container, a micro-VM, or a
    full system VM. Anything container-specific (image builds, networks,
    ``--userns`` mapping, OCI runtime selection) is deliberately kept *inside*
    the container implementation rather than promoted to a verb here, so adding
    a VM backend later is a new class and zero changes above this line. A wider
    interface would leak container vocabulary upward and make that impossible.

    A backend instance owns a single environment's lifecycle. ``destroy`` must
    be idempotent and safe to call after a partial ``create``, since the session
    orchestrator always runs it from a ``finally``.
    """

    @abstractmethod
    def create(self, spec: EnvironmentSpec) -> EnvironmentHandle:
        """Materialize the environment: network, images, containers (not running).

        Returns a handle describing everything created. Raises
        :class:`~tianluo.e2e.errors.E2EEnvironmentError` when the host cannot
        provide the environment (build failure, runtime error).
        """

    @abstractmethod
    def start(self, handle: EnvironmentHandle) -> None:
        """Start the created containers and block until every readiness probe
        passes (or the readiness budget is exhausted)."""

    @abstractmethod
    def exec(
        self,
        handle: EnvironmentHandle,
        service: str,
        argv: Sequence[str],
        *,
        timeout: Optional[float] = None,
        workdir: Optional[str] = None,
        environment: Optional[Mapping[str, str]] = None,
    ) -> ExecResult:
        """Run ``argv`` inside ``service`` and return its captured outcome.

        This is the single execution seam for scenario steps: CLI invocations,
        HTTP probes issued from inside the network, and browser-driver commands
        all funnel through it.
        """

    @abstractmethod
    def snapshot(
        self,
        handle: EnvironmentHandle,
        service: str,
        target: str,
        *,
        kind: str = "file",
        destination: Optional[Path] = None,
    ) -> Snapshot:
        """Extract ``target`` (a file path, or a screen for ``kind="screenshot"``)
        from ``service`` onto the host and describe where it landed."""

    @abstractmethod
    def destroy(self, handle: EnvironmentHandle) -> None:
        """Tear down everything in ``handle``; idempotent and never raises for
        resources that are already gone."""
