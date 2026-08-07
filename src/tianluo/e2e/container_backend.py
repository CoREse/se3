"""tianluo.e2e.container_backend — the docker/podman isolation backend.

The first (and currently only) :class:`~tianluo.e2e.backend.IsolationBackend`
implementation: a thin subprocess wrapper around the ``docker`` / ``podman``
CLIs. One environment is a shared network plus one container per declared
service; scenarios execute inside whichever service the scenario names as its
driver.

WHY a CLI wrapper instead of an SDK: docker and podman agree on the core verbs
(``build`` / ``pull`` / ``network`` / ``run`` / ``exec`` / ``cp`` / ``logs`` /
``rm``) closely enough that switching the *binary name* covers both runtimes
with one implementation, and machine-readable output is one ``--format`` away.
The alternatives each cost more than they give: ``docker-py`` has no BuildKit
support and is half-maintained, ``podman-py`` covers a single runtime, and
``testcontainers``' per-test container lifecycle is the wrong shape for a
long-lived multi-container environment reused across fix-loop iterations.

Everything here is stdlib-only; the ``tianluo[e2e]`` extra gates third-party
dependencies (image diffing), never the framework itself.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from tianluo.i18n import t

from . import readiness as readiness_mod
from .backend import (
    BindMount,
    EnvironmentHandle,
    EnvironmentSpec,
    ExecResult,
    IsolationBackend,
    ServiceSpec,
    Snapshot,
)
from .errors import E2EConfigError, E2EEnvironmentError
from .runtime_probe import RUNTIME_PODMAN, RuntimeProbeResult
from .templates import DEFAULT_WORKDIR, render_for_service

logger = logging.getLogger(__name__)

__all__ = [
    "ContainerBackend",
    "build_fingerprint",
    "container_name",
    "image_tag",
]

Runner = Callable[..., Any]

# Go-template form rather than the `--format json` shorthand: both CLIs have
# understood `{{json .}}` for far longer, so inspection parses on older
# docker/podman releases too (same choice as runtime_probe's info call).
JSON_FORMAT = "{{json .}}"

# A build or pull can legitimately take minutes on a cold cache; an unbounded
# wait would wedge the whole flow on a stuck registry.
DEFAULT_BUILD_TIMEOUT = 1800
DEFAULT_CLI_TIMEOUT = 300

# Container/image names accept a narrow character set; service names come from
# project configuration and are sanitized rather than rejected here (the schema
# layer is what reports a bad name to the author).
_NAME_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")

# Where a `kind="log"` snapshot's text is exposed, so a caller (readiness's
# timeout diagnostics) can read it without touching the filesystem.
_LOG_TEXT_KEY = "text"


def _sanitize(part: str) -> str:
    cleaned = _NAME_SAFE.sub("-", str(part)).strip("-._")
    return cleaned or "x"


def container_name(network: str, service: str) -> str:
    """Host-unique container name for ``service`` in the ``network`` environment.

    WHY the network prefix rather than the bare service name: service names come
    from project configuration and are generic by nature ("app", "db"), so two
    projects — or two worktrees of the same project — running e2e concurrently
    would collide on a plain ``--name app`` and the second ``run`` would fail.
    Service-name addressing between containers is delivered by the network
    alias instead (see :meth:`ContainerBackend.start`), which is per-network and
    therefore already isolated.
    """
    return "{}-{}".format(_sanitize(network), _sanitize(service))


def build_fingerprint(dockerfile: str) -> str:
    """Short stable digest of a rendered Dockerfile."""
    return hashlib.sha256(dockerfile.encode("utf-8")).hexdigest()[:12]


def image_tag(service: str, fingerprint: str) -> str:
    """Local tag for the image built for ``service``.

    WHY the build fingerprint rather than the environment's network name: the
    network carries the flow id (two concurrent worktree runs must not share a
    network), so a network-derived tag minted a brand-new image on *every* flow —
    one that no later flow could reuse and that nothing ever removed, since a
    tagged image survives `image prune`. Keying the tag on the rendered
    Dockerfile instead makes an unchanged environment resolve to the identical
    tag, so the next flow reuses the image it already has and only a genuine
    change to the build recipe mints a new one. Two projects whose recipes are
    byte-identical legitimately share the image: the source tree is bind-mounted
    at run time and is not part of what was built.
    """
    return "tianluo-e2e/{}:{}".format(_sanitize(service), _sanitize(fingerprint))


class ContainerBackend(IsolationBackend):
    """Run an e2e environment as a group of containers on a shared network.

    ``probe`` is the pinned :class:`~tianluo.e2e.runtime_probe.RuntimeProbeResult`
    for the session; its ``binary`` decides docker-vs-podman for every call, and
    it is never re-resolved mid-session (mixing runtimes would scatter the
    environment across two image stores and two network stacks).

    ``runner`` is the subprocess entry point, injectable so the whole backend is
    testable without a container runtime present. ``oci_runtime`` is passed
    through to ``--runtime``: a host with a VM-grade OCI runtime installed (Kata
    Containers and friends) upgrades e2e to VM-boundary isolation by
    configuration alone, with no second backend.
    """

    def __init__(
        self,
        probe: RuntimeProbeResult,
        *,
        runner: Runner = subprocess.run,
        oci_runtime: Optional[str] = None,
        selinux_label: bool = True,
        build_timeout: float = DEFAULT_BUILD_TIMEOUT,
        cli_timeout: float = DEFAULT_CLI_TIMEOUT,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.probe = probe
        self.runtime = probe.name
        self.binary = probe.binary or probe.name
        self._runner = runner
        self.oci_runtime = oci_runtime or None
        self.selinux_label = selinux_label
        self.build_timeout = build_timeout
        self.cli_timeout = cli_timeout
        self._env_overrides = dict(env or {})

    # ------------------------------------------------------------------
    # CLI plumbing
    # ------------------------------------------------------------------

    @property
    def is_podman(self) -> bool:
        return self.runtime == RUNTIME_PODMAN

    def _environ(self) -> Dict[str, str]:
        environ = dict(os.environ)
        if not self.is_podman:
            # BuildKit is what makes the per-step layer cache incremental across
            # fix-loop iterations; older docker defaults to the legacy builder,
            # where a changed step invalidates more than it should.
            environ.setdefault("DOCKER_BUILDKIT", "1")
        environ.update(self._env_overrides)
        return environ

    def _cli(
        self,
        *args: str,
        timeout: Optional[float] = None,
        stdin: Optional[str] = None,
    ) -> ExecResult:
        """Run one runtime CLI invocation and capture its outcome.

        Never raises for a non-zero exit — callers decide whether a failure is
        fatal (a build) or expected (removing an already-gone container), which
        is what makes ``destroy`` idempotent.
        """
        argv = [self.binary] + [str(a) for a in args]
        started = time.monotonic()
        try:
            completed = self._runner(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout if timeout is not None else self.cli_timeout,
                check=False,
                input=stdin,
                env=self._environ(),
            )
        except FileNotFoundError as exc:
            raise E2EEnvironmentError(
                t("e2e.backend.cli_missing", binary=self.binary),
                remediation=self.probe.remediation or t("e2e.probe.remediation"),
            ) from exc
        except subprocess.TimeoutExpired:
            return ExecResult(
                exit_code=124,
                stderr=t("e2e.backend.cli_timeout", command=" ".join(argv)),
                duration=time.monotonic() - started,
                timed_out=True,
            )
        except OSError as exc:
            raise E2EEnvironmentError(
                t("e2e.backend.cli_failed", binary=self.binary, detail=str(exc)),
                remediation=self.probe.remediation or t("e2e.probe.remediation"),
            ) from exc

        return ExecResult(
            exit_code=int(getattr(completed, "returncode", 1) or 0),
            stdout=getattr(completed, "stdout", "") or "",
            stderr=getattr(completed, "stderr", "") or "",
            duration=time.monotonic() - started,
        )

    @staticmethod
    def parse_json(text: str) -> Optional[Any]:
        """Parse ``{{json .}}`` output, tolerating empty and malformed results.

        WHY tolerant: inspection output is diagnostic, not load-bearing — a
        runtime that answers with an empty body (object already gone) or with a
        warning line prepended must not turn a working environment into a
        crash. Callers get ``None`` and fall back to what they already know.
        """
        stripped = (text or "").strip()
        if not stripped:
            return None
        try:
            return json.loads(stripped)
        except ValueError:
            # Some runtime/version combinations emit one JSON document per line
            # (and occasionally a stray warning first); salvage the first
            # parseable line rather than discarding everything.
            for line in stripped.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    return json.loads(line)
                except ValueError:
                    continue
            logger.debug("unparseable runtime JSON output: %r", stripped[:200])
            return None

    def inspect(self, target: str) -> Optional[Any]:
        """Inspect a container/image/network; ``None`` when absent or unparseable."""
        result = self._cli("inspect", "--format", JSON_FORMAT, target)
        if not result.ok:
            return None
        return self.parse_json(result.stdout)

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    def create(self, spec: EnvironmentSpec) -> EnvironmentHandle:
        """Create the shared network and every service image.

        Containers themselves are created by :meth:`start`: the network and the
        images are the *reusable* half of the environment (they survive a
        fix-loop iteration and are what the layer cache protects), while
        containers are per-run and are created and destroyed together with the
        run.
        """
        handle = EnvironmentHandle(runtime=self.runtime, spec=spec)
        # Published before the first host-side call: a build that fails on the
        # third service has already created the network and two images, and the
        # exception discards the return value, so the session can only find them
        # through here (see IsolationBackend.last_handle).
        self.last_handle = handle
        self._create_network(handle)
        for service in spec.services:
            handle.images[service.name] = self._materialize_image(handle, service)
        return handle

    def _create_network(self, handle: EnvironmentHandle) -> None:
        network = handle.spec.network
        result = self._cli("network", "create", network)
        if result.ok:
            handle.network_id = (result.stdout or "").strip() or network
            return
        combined = (result.stderr or "") + (result.stdout or "")
        if "already exists" in combined.lower():
            # A leftover network from an aborted run is reusable as-is; the
            # alternative (remove and recreate) would disconnect containers a
            # concurrent run legitimately owns.
            handle.network_id = network
            return
        raise E2EEnvironmentError(
            t(
                "e2e.backend.network_failed",
                network=network,
                detail=_detail(result.stderr or result.stdout),
            ),
            remediation=self.probe.remediation or t("e2e.probe.remediation"),
        )

    def _materialize_image(
        self, handle: EnvironmentHandle, service: ServiceSpec
    ) -> str:
        """Pull or build the image backing ``service`` and return its reference."""
        if not service.template and not service.build_steps:
            # External dependency services (databases, caches) use their
            # published image untouched — there is nothing project-specific to
            # layer on, and pulling beats rebuilding a copy of postgres:16.
            self._pull(service.base_image)
            return service.base_image
        return self._build(handle, service)

    def _pull(self, image: str) -> None:
        result = self._cli("pull", image, timeout=self.build_timeout)
        if result.ok:
            return
        combined = (result.stderr or "") + (result.stdout or "")
        if self._image_present(image):
            # A locally-present image with no registry access is still usable;
            # an offline host should not fail the whole environment.
            logger.debug("pull of %s failed but image is present locally", image)
            return
        raise E2EEnvironmentError(
            t("e2e.backend.pull_failed", image=image, detail=_detail(combined)),
            remediation=self.probe.remediation or t("e2e.probe.remediation"),
        )

    def _image_present(self, image: str) -> bool:
        return self._cli("image", "inspect", image).ok

    def _build(self, handle: EnvironmentHandle, service: ServiceSpec) -> str:
        dockerfile = render_for_service(
            service.template or "base",
            service.base_image,
            workdir=service.workdir or DEFAULT_WORKDIR,
            build_steps=service.build_steps,
        )
        tag = image_tag(service.name, build_fingerprint(dockerfile))
        # INVARIANT: the build context is a throwaway directory holding nothing
        # but the rendered Dockerfile — never the project root. The source tree
        # is bind-mounted at run time, so shipping it as build context would
        # both upload the whole repository to the builder on every rebuild and
        # tempt a future COPY back into the image.
        context_dir = Path(tempfile.mkdtemp(prefix="tianluo-e2e-build-"))
        try:
            dockerfile_path = context_dir / "Dockerfile"
            dockerfile_path.write_text(dockerfile, encoding="utf-8")
            args: List[str] = [
                "build",
                "-t",
                tag,
                "-f",
                str(dockerfile_path),
            ]
            for key, value in (handle.spec.labels or {}).items():
                args += ["--label", "{}={}".format(key, value)]
            args.append(str(context_dir))
            result = self._cli(*args, timeout=self.build_timeout)
        finally:
            shutil.rmtree(context_dir, ignore_errors=True)

        if not result.ok:
            raise E2EEnvironmentError(
                t(
                    "e2e.backend.build_failed",
                    service=service.name,
                    detail=_detail(result.stderr or result.stdout),
                ),
                remediation=self.probe.remediation or t("e2e.probe.remediation"),
            )
        return tag

    # ------------------------------------------------------------------
    # start
    # ------------------------------------------------------------------

    def start(self, handle: EnvironmentHandle) -> None:
        """Start every service container and block until each one is ready."""
        for service in handle.spec.services:
            self._run_service(handle, service)
        for service in handle.spec.services:
            if service.readiness is not None:
                readiness_mod.wait_ready(self, handle, service.name, service.readiness)
        handle.started = True

    def _run_service(self, handle: EnvironmentHandle, service: ServiceSpec) -> None:
        args = self.build_run_args(handle, service)
        result = self._cli(*args, timeout=self.cli_timeout)
        if not result.ok and _name_taken(result):
            # INVARIANT: a leftover container under this environment's *own*
            # deterministic name must never wedge the next run. The name is
            # `<network>-<service>` and the network carries the flow id, so every
            # iteration of one flow asks for the identical name — and with
            # `keep_environment` the session deliberately skips `destroy`, as does
            # a run killed between `start` and teardown. The runtime then rejects
            # `run` with a name conflict, which would surface as an environment
            # error carrying host-permission remediation ("join the docker
            # group") and abort a fix loop whose host is perfectly fine. Clearing
            # our own leftover and retrying is the same reuse tolerance
            # `_create_network` already applies to the network. Replace rather
            # than restart: the stale container still holds the previous
            # iteration's process state, and only a fresh one is guaranteed to be
            # running the code that is on the bind mount now.
            self._remove_stale_container(handle, service)
            result = self._cli(*args, timeout=self.cli_timeout)
        if not result.ok:
            raise E2EEnvironmentError(
                t(
                    "e2e.backend.run_failed",
                    service=service.name,
                    detail=_detail(result.stderr or result.stdout),
                ),
                remediation=self.probe.remediation or t("e2e.probe.remediation"),
            )
        handle.containers[service.name] = (result.stdout or "").strip() or (
            container_name(handle.spec.network, service.name)
        )

    def _remove_stale_container(
        self, handle: EnvironmentHandle, service: ServiceSpec
    ) -> None:
        """Drop the container squatting on this service's deterministic name.

        Scoped to that one name on purpose: it is a name this backend minted, so
        removing it can only ever discard our own leftover — never a container
        the user or a concurrent run owns.
        """
        name = container_name(handle.spec.network, service.name)
        removal = self._cli("rm", "-f", "-v", name)
        if removal.ok or _already_gone(removal):
            logger.info("replaced leftover e2e container %s", name)
            return
        # Not fatal here: the retry that follows produces the error the caller
        # should actually see, and it names the service.
        logger.warning(
            "could not remove leftover e2e container %s: %s",
            name,
            _detail(removal.stderr or removal.stdout),
        )

    def build_run_args(
        self, handle: EnvironmentHandle, service: ServiceSpec
    ) -> List[str]:
        """Assemble the ``run -d`` argv for ``service``.

        Split out from :meth:`_run_service` so the argv — the part that actually
        differs between runtimes — is inspectable without executing anything.
        """
        name = container_name(handle.spec.network, service.name)
        args: List[str] = [
            "run",
            "-d",
            "--name",
            name,
            # Service-name addressing between containers: peers resolve
            # `service.name` on the shared network regardless of the
            # collision-proof container name.
            "--network",
            handle.spec.network,
            "--network-alias",
            service.name,
            "--hostname",
            service.name,
        ]

        if self.oci_runtime:
            # A VM-grade OCI runtime (Kata and friends) upgrades the isolation
            # boundary without a second backend — configuration only.
            args += ["--runtime", self.oci_runtime]

        if self.is_podman:
            # INVARIANT: artifacts written into a bind mount must end up owned by
            # the host user who started the run. Rootless podman otherwise maps
            # the container's root onto a high subuid, so anything the container
            # writes into the mounted source tree lands as a foreign UID that the
            # invoking user can neither edit nor delete — leaving undeletable
            # residue in the project worktree. `--userns=keep-id` maps the host
            # UID onto itself inside the container, making ownership identical on
            # both sides. Rootless docker needs no equivalent flag: its daemon
            # already runs as the user, so container root *is* the host user; and
            # for rootful docker there is no unprivileged mapping to request.
            #
            # Unconditional for podman rather than gated on the probe's rootless
            # flag, for two reasons: podman under tianluo is always rootless (no
            # code path escalates, and rootful podman needs root), and the flag's
            # detection is best-effort — degrading to "no mapping" on an
            # unparseable `podman info` would reintroduce exactly the
            # undeletable-residue failure this line exists to prevent.
            args.append("--userns=keep-id")

        for key, value in (handle.spec.labels or {}).items():
            args += ["--label", "{}={}".format(key, value)]
        for key, value in (service.environment or {}).items():
            args += ["-e", "{}={}".format(key, value)]
        for port in service.ports:
            args += ["-p", str(port)]
        shared_sources = self._shared_mount_sources(handle)
        for mount in service.mounts:
            args += [
                "-v",
                self.format_mount(
                    mount, shared=_mount_key(mount) in shared_sources
                ),
            ]
        if service.workdir:
            args += ["-w", service.workdir]

        args.append(handle.images.get(service.name, service.base_image))
        args += list(service.command)
        return args

    def _shared_mount_sources(self, handle: EnvironmentHandle) -> set:
        """Host paths this environment mounts into more than one service."""
        counts: Dict[str, int] = {}
        for service in handle.spec.services:
            # A service mounting one path twice does not make it shared, so each
            # service contributes a given source at most once.
            for key in {_mount_key(mount) for mount in service.mounts}:
                counts[key] = counts.get(key, 0) + 1
        return {key for key, count in counts.items() if count > 1}

    def format_mount(self, mount: BindMount, *, shared: bool = False) -> str:
        """Render one bind mount as a ``-v`` argument.

        ``shared`` selects the SELinux relabel mode and is decided by the
        topology, not by the mount alone — see the comment below.
        """
        options: List[str] = []
        if mount.read_only:
            options.append("ro")
        if self.selinux_label:
            # SELinux relabelling. A no-op on hosts without SELinux, which is why
            # it is on by default; a project sharing the source tree with
            # SELinux-confined services can turn it off.
            #
            # INVARIANT: a host path mounted into more than one container gets
            # `:z` (shared), never `:Z`. `:Z` stamps a *private* MCS category on
            # the host path, so the second container to start would relabel the
            # project source out from under the first — the app container loses
            # access to /workspace the moment the browser container comes up, and
            # the resulting permission errors look exactly like application bugs
            # and get routed into the fix loop as code regressions. `:Z` stays for
            # genuinely single-container mounts, where the tighter label is free.
            options.append("z" if shared else "Z")
        spec = "{}:{}".format(Path(mount.source).as_posix(), mount.target)
        if options:
            spec += ":" + ",".join(options)
        return spec

    # ------------------------------------------------------------------
    # exec
    # ------------------------------------------------------------------

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
        """Run ``argv`` inside ``service``'s container."""
        target = self._require_container(handle, service)
        args: List[str] = ["exec"]
        if workdir:
            args += ["-w", workdir]
        for key, value in (environment or {}).items():
            args += ["-e", "{}={}".format(key, value)]
        args.append(target)
        args += [str(a) for a in argv]
        return self._cli(*args, timeout=timeout)

    def _require_container(self, handle: EnvironmentHandle, service: str) -> str:
        if handle.spec.service(service) is None:
            raise E2EConfigError(
                t(
                    "e2e.backend.unknown_service",
                    service=service,
                    known=", ".join(s.name for s in handle.spec.services) or "-",
                )
            )
        target = handle.containers.get(service)
        if not target:
            raise E2EEnvironmentError(
                t("e2e.backend.service_not_started", service=service)
            )
        return target

    # ------------------------------------------------------------------
    # snapshot
    # ------------------------------------------------------------------

    def snapshot(
        self,
        handle: EnvironmentHandle,
        service: str,
        target: str,
        *,
        kind: str = "file",
        destination: Optional[Path] = None,
    ) -> Snapshot:
        """Pull an artifact out of ``service`` onto the host.

        ``kind`` selects what is captured: ``"file"`` copies ``target`` out,
        ``"screenshot"`` captures the container's virtual display first (the
        GUI template ships ``scrot`` for exactly this) and then copies it, and
        ``"log"`` captures the container's log stream — whose text is also
        exposed via the snapshot's metadata so a caller needing only the tail
        (readiness diagnostics) does not have to read it back off disk.
        """
        container = self._require_container(handle, service)
        if kind == "log":
            return self._snapshot_log(handle, service, container, destination)
        if kind == "screenshot":
            return self._snapshot_screenshot(
                handle, service, container, target, destination
            )
        if kind == "file":
            return self._snapshot_file(service, container, target, destination)
        raise E2EConfigError(
            t("e2e.backend.unknown_snapshot_kind", kind=kind, service=service)
        )

    def _destination(self, destination: Optional[Path], suffix: str) -> Path:
        if destination is not None:
            path = Path(destination)
            path.parent.mkdir(parents=True, exist_ok=True)
            return path
        fd, name = tempfile.mkstemp(prefix="tianluo-e2e-", suffix=suffix)
        os.close(fd)
        return Path(name)

    def _snapshot_file(
        self,
        service: str,
        container: str,
        target: str,
        destination: Optional[Path],
    ) -> Snapshot:
        path = self._destination(destination, Path(target).suffix or ".bin")
        result = self._cli("cp", "{}:{}".format(container, target), str(path))
        if not result.ok:
            if destination is None:
                # The placeholder this method created is empty and nobody holds
                # a reference to it; leaving one behind per failed extraction
                # would slowly fill the host's temp filesystem.
                try:
                    path.unlink()
                except OSError:  # pragma: no cover - already gone
                    pass
            raise E2EEnvironmentError(
                t(
                    "e2e.backend.snapshot_failed",
                    service=service,
                    target=target,
                    detail=_detail(result.stderr or result.stdout),
                )
            )
        return Snapshot(kind="file", path=path, service=service)

    def _snapshot_screenshot(
        self,
        handle: EnvironmentHandle,
        service: str,
        container: str,
        target: str,
        destination: Optional[Path],
    ) -> Snapshot:
        remote = target or "/tmp/tianluo-e2e-screenshot.png"
        # `-o` overwrites, so repeated captures within one scenario do not pile
        # up numbered files inside the container.
        capture = self.exec(handle, service, ["scrot", "-o", remote])
        if not capture.ok:
            raise E2EEnvironmentError(
                t(
                    "e2e.backend.screenshot_failed",
                    service=service,
                    detail=_detail(capture.stderr or capture.stdout),
                )
            )
        snapshot = self._snapshot_file(service, container, remote, destination)
        return Snapshot(
            kind="screenshot",
            path=snapshot.path,
            service=service,
            metadata={"remote_path": remote},
        )

    def _snapshot_log(
        self,
        handle: EnvironmentHandle,
        service: str,
        container: str,
        destination: Optional[Path],
    ) -> Snapshot:
        result = self._cli("logs", container)
        text = (result.stdout or "") + (result.stderr or "")
        # INVARIANT: no destination means no host file. A `log` readiness probe
        # snapshots once per poll — dozens of times per service start — and the
        # caller reads the text straight out of `metadata`, so minting a temp
        # file per call would leak one file per poll with nothing to unlink it.
        # A caller that wants the log kept says so by naming a destination.
        path: Optional[Path] = None
        if destination is not None:
            path = self._destination(destination, ".log")
            try:
                path.write_text(text, encoding="utf-8")
            except OSError as exc:  # pragma: no cover - unwritable destination
                logger.debug("could not persist %s log: %s", service, exc)
        return Snapshot(
            kind="log",
            path=path,
            service=service,
            metadata={_LOG_TEXT_KEY: text},
        )

    # ------------------------------------------------------------------
    # destroy
    # ------------------------------------------------------------------

    def destroy(self, handle: EnvironmentHandle) -> None:
        """Remove every container and the shared network.

        Images are deliberately left behind: they are the reusable half of the
        environment and their tags are keyed to the build recipe (see
        :func:`image_tag`), so the next flow with an unchanged environment
        reuses them instead of rebuilding. Deleting them here would make every
        run pay a full rebuild for a cache that is correct by construction.

        INVARIANT: idempotent and never raising. The session orchestrator calls
        this from a ``finally``, including after a half-finished ``create``, so
        a resource that is already gone — or was never created — must be a
        no-op rather than masking the original failure with a teardown error.

        INVARIANT: removal is driven by the *declared* services, not only by the
        containers ``start`` managed to record. A `run` that creates a container
        but fails to start it, or a previous run killed between `start` and
        `destroy`, leaves a container under the deterministic
        ``<network>-<service>`` name that nothing recorded — and since the name
        is stable per flow, the next run's `run` then fails with "name already
        in use" and the network cannot be removed for having active endpoints,
        wedging every subsequent iteration. Removing an object that was never
        created is already a no-op here, so sweeping the full topology costs one
        cheap call per service and makes that dead end unreachable.
        """
        for target in self._removal_targets(handle):
            result = self._cli("rm", "-f", "-v", target)
            if not result.ok and not _already_gone(result):
                logger.warning(
                    "could not remove e2e container %s: %s",
                    target,
                    _detail(result.stderr or result.stdout),
                )
        handle.containers.clear()

        if handle.network_id or handle.spec.network:
            result = self._cli("network", "rm", handle.spec.network)
            if not result.ok and not _already_gone(result):
                logger.warning(
                    "could not remove e2e network %s: %s",
                    handle.spec.network,
                    _detail(result.stderr or result.stdout),
                )
            handle.network_id = None

        handle.started = False

    def _removal_targets(self, handle: EnvironmentHandle) -> List[str]:
        """Every container reference ``destroy`` must try, in a stable order.

        The recorded id when ``start`` got that far, the deterministic name
        otherwise — plus anything recorded under a service the spec no longer
        declares, so an edited topology cannot strand a live container.
        """
        targets: List[str] = []
        seen: set = set()

        def add(target: str) -> None:
            if target and target not in seen:
                seen.add(target)
                targets.append(target)

        for service in handle.spec.services:
            add(
                handle.containers.get(service.name)
                or container_name(handle.spec.network, service.name)
            )
        for name, recorded in handle.containers.items():
            add(recorded or container_name(handle.spec.network, name))
        return targets


def _mount_key(mount: BindMount) -> str:
    """Identity of a bind mount's *host* side, for sharing detection."""
    return Path(mount.source).as_posix()


_NAME_TAKEN = re.compile(
    r"(container name\b[^\n]*\balready in use"
    r"|already in use by container"
    r"|name is already in use"
    r"|container\b[^\n]*\balready exists)",
    re.IGNORECASE,
)


def _name_taken(result: ExecResult) -> bool:
    """Whether ``run`` failed only because the container name is occupied.

    Deliberately narrower than a bare "already in use" substring: a service that
    publishes a host port already bound by something else fails with "port is
    already allocated"/"address already in use", and that is a genuine conflict
    with a foreign process — retrying it after removing our container would just
    hide the real cause behind a second identical failure.
    """
    text = (result.stderr or "") + (result.stdout or "")
    return bool(_NAME_TAKEN.search(text))


def _already_gone(result: ExecResult) -> bool:
    """Whether a removal failed only because the resource no longer exists."""
    text = ((result.stderr or "") + (result.stdout or "")).lower()
    return any(
        marker in text
        for marker in ("no such", "not found", "does not exist", "already removed")
    )


def _detail(text: Optional[str], limit: int = 600) -> str:
    """Collapse a captured stream into a short single-paragraph detail."""
    cleaned = " ".join((text or "").split())
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + "…"
    return cleaned
