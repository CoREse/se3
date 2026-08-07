"""tianluo.e2e.runtime_probe — container-runtime detection and e2e preflight.

Resolves ``e2e.runtime`` (``auto`` / ``docker`` / ``podman``) into the one
runtime a whole e2e session will use, by *executing* ``docker info`` /
``podman info`` rather than looking the binary up on ``PATH``.

The same function is the preflight check: an ``info`` call that exits 0 proves,
in one shot, that the binary exists, that the daemon/runtime environment is
healthy, and that the *current user* may drive it. tianluo runs entirely
unprivileged — no code path escalates — so "the user can run ``docker`` or
``podman`` directly" is exactly the precondition worth testing, and exactly what
``info`` tests.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from tianluo.i18n import t

from .errors import E2EConfigError, E2EEnvironmentError

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_PROBE_TIMEOUT",
    "RUNTIME_AUTO",
    "RUNTIME_DOCKER",
    "RUNTIME_PODMAN",
    "RuntimeProbeResult",
    "SUPPORTED_RUNTIMES",
    "normalize_preference",
    "preflight",
    "probe_one",
    "probe_runtime",
]

RUNTIME_AUTO = "auto"
RUNTIME_DOCKER = "docker"
RUNTIME_PODMAN = "podman"

SUPPORTED_RUNTIMES = (RUNTIME_DOCKER, RUNTIME_PODMAN)

# WHY: `auto` probes docker before podman as a *deterministic* preference, not a
# quality judgement. On a machine with both installed, Docker is usually the one
# the user deliberately set up for daily work (and has the most mature
# BuildKit/buildx ecosystem), so picking it is the least surprising default;
# anyone who wants podman first states `runtime: podman` explicitly.
_AUTO_ORDER = (RUNTIME_DOCKER, RUNTIME_PODMAN)

# `docker info` can take a while on a cold daemon, but an unbounded wait would
# hang the whole flow on a wedged runtime.
DEFAULT_PROBE_TIMEOUT = 30

# Go-template form rather than `--format json`: both CLIs have supported
# `{{json .}}` far longer than the `json` shorthand, so this parses on older
# docker/podman releases too.
_INFO_FORMAT = "{{json .}}"

# Keep failure details readable when they land in a step's error_message.
_DETAIL_LIMIT = 400

Runner = Callable[..., Any]


@dataclass(frozen=True)
class RuntimeProbeResult:
    """Outcome of probing one container runtime.

    Frozen because the session pins one result and reuses it for every
    subsequent container operation: mixing runtimes mid-run would scatter
    containers across two independent image stores and network stacks.
    """

    name: str
    binary: str
    ok: bool
    rootless: bool = False
    error: str = ""
    remediation: str = ""
    error_key: str = ""
    remediation_key: str = ""

    def __bool__(self) -> bool:
        return self.ok


def _remediation_text() -> str:
    """The full menu of ways to get an unprivileged container runtime working."""
    return t("e2e.probe.remediation")


def _detail(text: str) -> str:
    """Collapse a captured stream into a short single-paragraph detail."""
    cleaned = " ".join((text or "").split())
    if len(cleaned) > _DETAIL_LIMIT:
        cleaned = cleaned[:_DETAIL_LIMIT] + "…"
    return cleaned


def _parse_rootless(name: str, stdout: str) -> bool:
    """Best-effort read of whether the runtime is running rootless.

    Purely informational (it feeds diagnostics and the bind-mount UID-mapping
    decision); a runtime whose ``info`` output cannot be parsed is still usable,
    so parse failures degrade to ``False`` rather than failing the probe.
    """
    try:
        data = json.loads(stdout or "")
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    if name == RUNTIME_PODMAN:
        host = data.get("host")
        security = host.get("security") if isinstance(host, dict) else None
        if isinstance(security, dict):
            return bool(security.get("rootless", False))
        return False
    options = data.get("SecurityOptions")
    if isinstance(options, (list, tuple)):
        if any("rootless" in str(opt) for opt in options):
            return True
    client = data.get("ClientInfo")
    if isinstance(client, dict) and str(client.get("Context", "")) == "rootless":
        return True
    return False


def probe_one(
    name: str,
    *,
    runner: Runner = subprocess.run,
    timeout: int = DEFAULT_PROBE_TIMEOUT,
) -> RuntimeProbeResult:
    """Probe a single runtime by running ``<name> info`` as the current user.

    Never raises for an unusable runtime — it reports ``ok=False`` with a
    localized ``error`` plus ``remediation``, so callers that want to *display*
    every candidate (``luo e2e doctor``) and callers that want to *select* one
    (:func:`probe_runtime`) can share this one code path.
    """
    argv = [name, "info", "--format", _INFO_FORMAT]
    try:
        completed = runner(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return _failure(name, "e2e.probe.not_installed", runtime=name)
    except subprocess.TimeoutExpired:
        return _failure(name, "e2e.probe.timeout", runtime=name, timeout=timeout)
    except OSError as exc:
        return _failure(
            name, "e2e.probe.launch_failed", runtime=name, detail=_detail(str(exc))
        )

    returncode = getattr(completed, "returncode", 1)
    stdout = getattr(completed, "stdout", "") or ""
    stderr = getattr(completed, "stderr", "") or ""
    if returncode != 0:
        # The overwhelmingly common cause here is a permission problem (user not
        # in the `docker` group, or a socket the daemon owns) — indistinguishable
        # from "daemon down" at this level, and both have the same fix menu.
        detail = _detail(stderr) or _detail(stdout)
        return _failure(
            name,
            "e2e.probe.failed",
            runtime=name,
            code=returncode,
            detail=detail,
        )

    return RuntimeProbeResult(
        name=name,
        binary=name,
        ok=True,
        rootless=_parse_rootless(name, stdout),
    )


def _failure(name: str, error_key: str, **fields: Any) -> RuntimeProbeResult:
    return RuntimeProbeResult(
        name=name,
        binary=name,
        ok=False,
        error=t(error_key, **fields),
        remediation=_remediation_text(),
        error_key=error_key,
        remediation_key="e2e.probe.remediation",
    )


def normalize_preference(preference: Optional[str]) -> str:
    """Normalize a raw ``e2e.runtime`` value; empty/``None`` means ``auto``."""
    if preference is None:
        return RUNTIME_AUTO
    if not isinstance(preference, str):
        raise E2EConfigError(
            t("e2e.probe.invalid_preference", value=repr(preference))
        )
    normalized = preference.strip().lower()
    if not normalized:
        return RUNTIME_AUTO
    if normalized != RUNTIME_AUTO and normalized not in SUPPORTED_RUNTIMES:
        raise E2EConfigError(
            t("e2e.probe.invalid_preference", value=preference)
        )
    return normalized


def probe_runtime(
    preference: Optional[str] = RUNTIME_AUTO,
    *,
    runner: Runner = subprocess.run,
    timeout: int = DEFAULT_PROBE_TIMEOUT,
) -> RuntimeProbeResult:
    """Resolve ``preference`` into the runtime this session will use.

    ``auto`` probes docker then podman and takes the first usable one. An
    explicit ``docker``/``podman`` is probed alone.

    Raises :class:`~tianluo.e2e.errors.E2EEnvironmentError` when no usable
    runtime is found, and :class:`~tianluo.e2e.errors.E2EConfigError` for an
    unrecognized preference value.
    """
    resolved = normalize_preference(preference)

    if resolved != RUNTIME_AUTO:
        # WHY: an explicitly configured runtime NEVER silently falls back to the
        # other one — not even when the other is sitting right there and
        # working. Swapping runtimes behind the user's back changes the image
        # cache, the storage location and the UID-mapping behaviour all at once,
        # producing "it worked yesterday" failures that are near-impossible to
        # trace back to the swap. We probe only the requested runtime and fail
        # loudly with the fix menu.
        result = probe_one(resolved, runner=runner, timeout=timeout)
        if result.ok:
            logger.debug("e2e runtime %s selected (explicit)", result.name)
            return result
        raise E2EEnvironmentError(
            t(
                "e2e.probe.explicit_unavailable",
                runtime=resolved,
                detail=result.error,
            ),
            remediation=result.remediation,
        )

    attempts: List[RuntimeProbeResult] = []
    for candidate in _AUTO_ORDER:
        result = probe_one(candidate, runner=runner, timeout=timeout)
        if result.ok:
            logger.debug("e2e runtime %s selected (auto)", result.name)
            return result
        # "Installed but unusable by this user" counts as absent, so probing
        # continues to the next candidate instead of stopping at a runtime that
        # would fail on the very first container operation.
        attempts.append(result)

    tried = "; ".join(f"{r.name}: {r.error}" for r in attempts)
    raise E2EEnvironmentError(
        t("e2e.probe.none_available", tried=tried),
        remediation=_remediation_text(),
    )


def preflight(
    config: Any = None,
    *,
    runner: Runner = subprocess.run,
    timeout: int = DEFAULT_PROBE_TIMEOUT,
) -> RuntimeProbeResult:
    """Run the e2e environment preflight and return the pinned runtime.

    ``config`` may be an ``E2EConfig`` (anything exposing a ``runtime``
    attribute), a bare preference string, or ``None`` for ``auto``. WHY the
    duck-typed signature: this is the bottom layer of the e2e stack and is
    imported by the config layer's consumers, not the other way round — keeping
    it structurally typed avoids an import cycle and lets the CLI's ``doctor``
    path call it with nothing but a string.

    Preflight failure is an *environment* problem: the caller lets the resulting
    :class:`~tianluo.e2e.errors.E2EEnvironmentError` reach the step handler,
    which reports FAILED with remediation instead of entering the fix loop.
    """
    if config is None:
        preference: Optional[str] = RUNTIME_AUTO
    elif isinstance(config, str):
        preference = config
    else:
        preference = getattr(config, "runtime", RUNTIME_AUTO)
    return probe_runtime(preference, runner=runner, timeout=timeout)
