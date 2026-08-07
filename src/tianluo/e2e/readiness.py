"""tianluo.e2e.readiness — "is this service actually up?" probing.

A container that is *running* is not a service that is *usable*: a database
spends seconds initializing, a web server binds its port after its framework
boots. Starting a scenario against a not-yet-ready service produces a failure
that looks exactly like a code defect and would be routed into the fix loop —
so readiness is checked before a single scenario runs, and a service that never
becomes ready is reported as an *environment* error instead.

Four probe kinds, matching :class:`~tianluo.e2e.backend.ReadinessProbe`:

``command``
    Run a command inside the container until it exits 0. The most portable
    kind, and the only one that observes the service from inside the network.
``http``
    GET a URL until it answers with an acceptable status.
``tcp``
    Open a TCP connection to a port.
``log``
    Wait for a regular expression to appear in the container's log stream.

Stdlib only — this module sits on the core path and must stay importable
without the ``tianluo[e2e]`` extra.
"""

from __future__ import annotations

import logging
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Callable, Optional
from urllib.parse import urlparse

from tianluo.i18n import t

from .backend import EnvironmentHandle, IsolationBackend, ReadinessProbe
from .errors import E2EConfigError, E2EEnvironmentError

logger = logging.getLogger(__name__)

__all__ = [
    "PROBE_KINDS",
    "read_log_tail",
    "wait_ready",
]

PROBE_KINDS = ("command", "http", "tcp", "log")

# Status codes an `http` probe accepts when the probe itself names none. Any 2xx
# or 3xx means "the server answered", which is what readiness asks; a 404 does
# not, because it usually means the app is up but the health route is wrong and
# the author should hear about that rather than have it pass silently. A service
# whose healthy answer falls outside that range (an auth-guarded root replying
# 401) states its own `status:` instead.
_DEFAULT_OK_STATUS = tuple(range(200, 400))

# How much of the service log accompanies a readiness timeout. Enough to show
# the crash/stack that kept the service from coming up, short enough to stay
# readable inside a step's error message.
LOG_TAIL_LINES = 30

_LOG_TEXT_KEY = "text"


def read_log_tail(
    backend: IsolationBackend,
    handle: EnvironmentHandle,
    service: str,
    *,
    lines: int = LOG_TAIL_LINES,
) -> str:
    """Return the tail of ``service``'s log, or ``""`` if it cannot be read.

    Goes through the backend's ``snapshot(kind="log")`` verb rather than a
    runtime-specific ``logs`` call, so the diagnostic works for any backend that
    satisfies the narrow interface — a VM backend included. Never raises: this
    only ever runs while *reporting* another failure, and losing the diagnostic
    must not replace it.

    WHY no destination is passed: a `log` probe calls this once per poll, so a
    persisted file per call would accumulate on the host for text that is read
    once and thrown away. The backend answers with the text in ``metadata`` and
    writes nothing; the ``path`` fallback below only serves a backend that
    delivers the log as a file it already owns.
    """
    try:
        snapshot = backend.snapshot(handle, service, "", kind="log")
    except Exception as exc:  # pragma: no cover - depends on backend
        logger.debug("could not capture %s log for diagnostics: %s", service, exc)
        return ""
    text = ""
    metadata = getattr(snapshot, "metadata", None) or {}
    if isinstance(metadata, dict):
        text = str(metadata.get(_LOG_TEXT_KEY) or "")
    if not text and getattr(snapshot, "path", None) is not None:
        try:
            text = snapshot.path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:  # pragma: no cover - unreadable temp file
            logger.debug("could not read %s log snapshot: %s", service, exc)
            return ""
    tail = text.splitlines()[-lines:]
    return "\n".join(tail)


def _check_command(
    backend: IsolationBackend,
    handle: EnvironmentHandle,
    service: str,
    probe: ReadinessProbe,
    attempt_timeout: float,
) -> bool:
    if not probe.command:
        raise E2EConfigError(
            t(
                "e2e.readiness.missing_field",
                service=service,
                kind="command",
                field="command",
            )
        )
    result = backend.exec(
        handle, service, list(probe.command), timeout=attempt_timeout
    )
    return bool(getattr(result, "ok", False))


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Makes urllib surface a redirect instead of following it.

    Returning ``None`` from ``redirect_request`` leaves the 3xx to propagate as
    an ``HTTPError``, which is exactly the observation a probe declaring
    ``status: 302`` is asking for.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _open_http(url: str, probe: ReadinessProbe, timeout: float):
    """Open ``url`` the way this probe's declared status can be observed.

    WHY the branch: the default opener follows redirects transparently, so the
    status a probe sees is always the terminal one. A service whose healthy
    answer *is* the redirect (a 302 to a login page as the "up" signal) could
    then never match its own declared ``status:`` — the probe would compare the
    wrong code on every attempt, spend its whole budget and report a healthy
    service as an environment failure. Only that case gets a private opener; the
    ordinary probe keeps ``urlopen`` and its default handler chain.
    """
    if probe.status is not None and 300 <= int(probe.status) < 400:
        opener = urllib.request.build_opener(_NoRedirectHandler)
        return opener.open(url, timeout=timeout)
    return urllib.request.urlopen(url, timeout=timeout)


def _check_http(service: str, probe: ReadinessProbe, attempt_timeout: float) -> bool:
    if not probe.url:
        raise E2EConfigError(
            t("e2e.readiness.missing_field", service=service, kind="http", field="url")
        )
    # WHY the request leaves from the host: an `http` probe targets a port the
    # service publishes to the host, which is the only address the host can
    # reach before any container exists to run a request from. A service that is
    # only reachable *inside* the network uses a `command` probe instead (curl
    # from a peer container), which is why both kinds exist — and why
    # `config_schema` rejects an `http` probe aimed at an in-network address.
    try:
        with _open_http(probe.url, probe, attempt_timeout) as response:
            status = int(getattr(response, "status", 0) or response.getcode() or 0)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
    except (urllib.error.URLError, OSError, ValueError):
        return False
    if probe.status is not None:
        return status == int(probe.status)
    return status in _DEFAULT_OK_STATUS


def _check_tcp(service: str, probe: ReadinessProbe, attempt_timeout: float) -> bool:
    if not probe.port:
        raise E2EConfigError(
            t("e2e.readiness.missing_field", service=service, kind="tcp", field="port")
        )
    host = _tcp_host(probe)
    try:
        with socket.create_connection((host, int(probe.port)), attempt_timeout):
            return True
    except OSError:
        return False


def _tcp_host(probe: ReadinessProbe) -> str:
    """Host a `tcp` probe dials — the published port on the host by default."""
    if probe.url:
        parsed = urlparse(probe.url if "//" in probe.url else "//" + probe.url)
        if parsed.hostname:
            return parsed.hostname
    return "127.0.0.1"


def _check_log(
    backend: IsolationBackend,
    handle: EnvironmentHandle,
    service: str,
    probe: ReadinessProbe,
) -> bool:
    if not probe.pattern:
        raise E2EConfigError(
            t(
                "e2e.readiness.missing_field",
                service=service,
                kind="log",
                field="pattern",
            )
        )
    try:
        matcher = re.compile(probe.pattern)
    except re.error as exc:
        raise E2EConfigError(
            t(
                "e2e.readiness.bad_pattern",
                service=service,
                pattern=probe.pattern,
                detail=str(exc),
            )
        ) from exc
    text = read_log_tail(backend, handle, service, lines=100000)
    return bool(matcher.search(text))


def wait_ready(
    backend: IsolationBackend,
    handle: EnvironmentHandle,
    service: str,
    probe: Optional[ReadinessProbe],
    deadline: Optional[float] = None,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    """Block until ``service`` passes ``probe``, or raise on exhaustion.

    ``deadline`` is an absolute ``clock()`` value; when omitted it is derived
    from the probe's own ``timeout`` budget. ``clock``/``sleeper`` are injected
    so the polling loop is testable without real time passing.

    Returns ``True`` once the probe passes. A service with no probe is ready as
    soon as its container is running, which is what ``probe=None`` means.

    Raises :class:`~tianluo.e2e.errors.E2EEnvironmentError` on timeout — a
    service that never comes up is an environment problem, so the E2E step
    reports FAILED with this diagnostic instead of dispatching the fix loop to
    "repair" code that was never reached. Raises
    :class:`~tianluo.e2e.errors.E2EConfigError` for an unknown probe kind:
    silently treating a typo'd kind as ready would let scenarios run against a
    service nobody ever checked.
    """
    if probe is None:
        return True

    kind = (probe.kind or "").strip().lower()
    if kind not in PROBE_KINDS:
        raise E2EConfigError(
            t(
                "e2e.readiness.unknown_kind",
                kind=probe.kind,
                service=service,
                known=", ".join(PROBE_KINDS),
            )
        )

    started = clock()
    if deadline is None:
        deadline = started + max(float(probe.timeout or 0.0), 0.0)
    interval = max(float(probe.interval or 0.0), 0.0)
    attempts = 0
    last_error = ""

    while True:
        attempts += 1
        remaining = deadline - clock()
        # Floored at 1s: a sub-second per-attempt timeout makes every probe
        # report a timeout of its own, hiding the fact that it was the *budget*
        # that ran out. The overshoot it can cause is bounded by that one second;
        # the sleep below is what has to respect the budget exactly.
        attempt_timeout = max(min(remaining, float(probe.timeout or 0.0)), 1.0)
        try:
            if kind == "command":
                passed = _check_command(
                    backend, handle, service, probe, attempt_timeout
                )
            elif kind == "http":
                passed = _check_http(service, probe, attempt_timeout)
            elif kind == "tcp":
                passed = _check_tcp(service, probe, attempt_timeout)
            else:
                passed = _check_log(backend, handle, service, probe)
        except (E2EConfigError, E2EEnvironmentError):
            raise
        except Exception as exc:
            # A transient failure of the probe mechanism itself (a container not
            # yet accepting exec) is indistinguishable from "not ready yet" and
            # must not abort the wait before the budget is spent.
            passed = False
            last_error = str(exc)
            logger.debug("readiness probe error for %s: %s", service, exc)

        if passed:
            logger.debug(
                "service %s ready after %d attempt(s), %.1fs",
                service,
                attempts,
                clock() - started,
            )
            return True

        if clock() >= deadline:
            break
        # INVARIANT: the wait never outlasts its declared budget. Sleeping the
        # full interval regardless of what is left would let a probe with a long
        # interval and a short timeout (interval 30, timeout 5) run six times
        # over budget before reporting the environment failure — the budget is
        # what the caller sized the whole e2e step against.
        sleeper(max(min(interval, deadline - clock()), 0.0))
        if clock() >= deadline:
            break

    tail = read_log_tail(backend, handle, service)
    raise E2EEnvironmentError(
        t(
            "e2e.readiness.timeout",
            service=service,
            kind=kind,
            seconds=round(max(clock() - started, 0.0), 1),
            attempts=attempts,
            detail=last_error or "-",
            log=tail or t("e2e.readiness.no_log"),
        ),
        remediation=t("e2e.readiness.remediation", service=service),
    )
