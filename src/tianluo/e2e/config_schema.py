"""tianluo.e2e.config_schema — declarative schema for the ``tianluo/e2e/`` content.

:func:`validate_content` takes the *raw* documents (as read from disk, or as
just produced by the bootstrap step before anything is written) and returns a
list of human-readable problems. An empty list means the content is admissible;
the caller raises :class:`~tianluo.e2e.errors.E2EConfigError` with the joined
list otherwise. Returning rather than raising lets one pass report *every*
problem in a generated document instead of making the author fix them one
round-trip at a time.

INVARIANT: the assertion ladder is enforced *here*, in schema validation, not
merely described in the scenario-authoring prompt. The ladder says a check must
use the lowest tier that can express it — tier 1 deterministic assertions (exit
code, stream match, HTTP response, file artifact, DOM query) by default; tier 2
baseline screenshot diff only when the subject genuinely *is* a visual
rendering; tier 3 LLM-looks-at-image only as a declared last resort, and only
with a reviewable evidence description. A prompt-only rule has no enforcement:
an LLM writing scenarios drifts systematically toward "just look at the
screenshot", which quietly converts deterministic verification into
probabilistic verification, and the drift is invisible until someone audits the
scenarios by hand. Encoded as validation, a violation fails at parse time with a
locatable message, before a single container is built.

Pure function, stdlib only: no container, no network, and no IO beyond the
optional baseline-file existence check (which is skipped entirely when the
caller passes no ``baselines_dir``, or passes
``require_existing_baselines=False`` — the first-capture path, see
:func:`_check_baseline`). No third-party validation library either —
the rules below are specific enough that a jsonschema document would be both
larger and unable to express the ladder checks.
"""

from __future__ import annotations

import math
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

from tianluo.i18n import t

__all__ = [
    "ACTION_KINDS",
    "BASE_KINDS",
    "DETERMINISTIC_ASSERTIONS",
    "PLAYWRIGHT_BASE_KIND",
    "READINESS_KINDS",
    "SEMANTIC_VISUAL_ASSERTIONS",
    "SERVICE_NAME_PATTERN",
    "VISUAL_REGRESSION_ASSERTIONS",
    "baseline_is_contained",
    "service_base_kinds",
    "validate_content",
    "validate_environment",
    "validate_scenario",
]

# A service name doubles as its container name *and* as the DNS name peers use
# on the shared network, so it must satisfy the stricter of the two: a DNS
# label.
SERVICE_NAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

# The three base-image sources the design distinguishes, named after the
# Dockerfile template family each one selects.
BASE_KINDS = ("base", "playwright", "gui-xvfb")

# The base kind that ships a browser, and therefore the only driver that can
# execute a `browser` action or answer a `dom` query.
PLAYWRIGHT_BASE_KIND = "playwright"

# Scenario constructs the executor can only serve from a Playwright driver.
_BROWSER_ONLY_ACTIONS = ("browser",)
_BROWSER_ONLY_ASSERTIONS = ("dom",)

READINESS_KINDS = ("command", "http", "tcp", "log")

# Addresses that mean "this host" to a probe issued by the tianluo process.
# WHY the distinction matters: `http` and `tcp` probes are dialled from the
# host, so they can only ever reach a *published* port; `command` probes run
# inside the container and see the network. A probe pointed at an in-network
# address is not slow — it is unreachable, and would burn its whole timeout
# before reporting an environment failure for a service that was healthy all
# along. Both halves of that trap are rejected below.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"})

# kind -> (always-required fields, at-least-one-of fields)
_READINESS_FIELDS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "command": (("command",), ()),
    "http": (("url",), ()),
    "tcp": (("port",), ()),
    "log": (("pattern",), ()),
}

# Field -> value shape, checked whenever the field is present. WHY presence is
# not enough: the conversion layer turns these straight into a ReadinessProbe
# (`int(port)`, `tuple(str(part) for part in command)`), so a document that only
# had its *names* vetted reaches `int("abc")` and the E2E step dies with a bare
# ValueError traceback instead of a located configuration error the author can
# act on.
_READINESS_FIELD_SHAPES: Dict[str, str] = {
    "command": "argv",
    "url": "string",
    "pattern": "string",
    "port": "port",
}

_ACTION_FIELDS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "exec": (("command",), ()),
    "http": (("url",), ()),
    "browser": (("op",), ()),
    "wait": ((), ("seconds", "until")),
    "screenshot": (("name",), ()),
    # Coordinate-driven input: the operation-side counterpart of a tier-3
    # assertion, and gated the same way.
    "visual_click": (("x", "y"), ()),
}
ACTION_KINDS = tuple(_ACTION_FIELDS)

# Tier 1 — deterministic. The default; no declaration needed.
DETERMINISTIC_ASSERTIONS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "exit_code": ((), ("equals",)),
    "stdout": ((), ("matches", "contains", "equals")),
    "stderr": ((), ("matches", "contains", "equals")),
    "http_status": (("url",), ("equals",)),
    "http_body": (("url",), ("matches", "contains", "equals")),
    "file_exists": (("path",), ()),
    "file_content": (("path",), ("matches", "contains", "equals")),
    "dom": (("selector",), ()),
}

# Tier 2 — deterministic diff against a git-tracked baseline image.
VISUAL_REGRESSION_ASSERTIONS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "screenshot_diff": (("baseline",), ()),
}

# Tier 3 — an LLM inspects an image. Last resort.
SEMANTIC_VISUAL_ASSERTIONS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "visual_semantic": (("question",), ()),
}

# Fields whose presence proves a lower-tier assertion could have done the job.
# A textual expectation is checkable with a stream/DOM assertion at tier 1; a
# selector proves a programmatic entry point into the UI exists, which rules out
# asking an LLM to look at a picture. `selector` is *not* held against tier 2,
# where it legitimately scopes the diff to one rendered region.
_TIER2_DOWNGRADE_FIELDS = ("text",)
_TIER3_DOWNGRADE_FIELDS = ("selector", "text")


def _type_name(value: Any) -> str:
    return type(value).__name__


def _err(errors: List[str], source: str, path: str, key: str, **fields: Any) -> None:
    """Append one located problem.

    Every message is prefixed with the file it came from and the YAML path
    inside it, because these documents are machine-generated: without a precise
    anchor an author cannot tell which of six near-identical scenario blocks is
    the offender.
    """
    errors.append(f"{source}: {path}: {t(key, **fields)}")


def _require_mapping(
    value: Any, errors: List[str], source: str, path: str
) -> Optional[Mapping[str, Any]]:
    if not isinstance(value, Mapping):
        _err(errors, source, path, "e2e.schema.not_mapping", actual=_type_name(value))
        return None
    return value


def _require_string(
    value: Any, errors: List[str], source: str, path: str, *, allow_missing: bool = False
) -> Optional[str]:
    if value is None:
        if not allow_missing:
            _err(errors, source, path, "e2e.schema.missing")
        return None
    if not isinstance(value, str):
        _err(errors, source, path, "e2e.schema.not_string", actual=_type_name(value))
        return None
    if not value.strip():
        _err(errors, source, path, "e2e.schema.empty")
        return None
    return value


def _require_positive(value: Any, errors: List[str], source: str, path: str) -> None:
    # WHY finiteness is part of "positive": YAML admits `.inf` and `.nan`, and
    # neither is caught by a `<= 0` test — NaN compares false against everything
    # and inf is enthusiastically positive. Both then reach the conversion layer,
    # where `int(math.ceil(...))` dies with a bare OverflowError/ValueError
    # traceback instead of the located configuration error this vetting exists to
    # produce, and a NaN readiness budget yields a deadline no comparison can
    # ever satisfy.
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        _err(errors, source, path, "e2e.schema.not_positive", value=repr(value))
        return
    if value <= 0:
        _err(errors, source, path, "e2e.schema.not_positive", value=value)


def _check_choice(
    value: Any, choices: Sequence[str], errors: List[str], source: str, path: str
) -> bool:
    if value not in choices:
        _err(
            errors, source, path, "e2e.schema.invalid_choice",
            value=value, choices=", ".join(choices),
        )
        return False
    return True


def _check_required_fields(
    entry: Mapping[str, Any],
    spec: Tuple[Tuple[str, ...], Tuple[str, ...]],
    errors: List[str],
    source: str,
    path: str,
    context: str,
) -> None:
    """Enforce a (always-required, at-least-one-of) field spec."""
    always, one_of = spec
    for name in always:
        if entry.get(name) is None:
            _err(
                errors, source, f"{path}.{name}",
                "e2e.schema.required_for", context=context,
            )
    if one_of and not any(entry.get(name) is not None for name in one_of):
        _err(
            errors, source, path, "e2e.schema.required_one_of",
            fields=", ".join(one_of), context=context,
        )


def validate_environment(raw: Any, source: str) -> Tuple[List[str], List[str]]:
    """Validate ``environment.yaml``.

    Returns ``(errors, service_names)``; the names are handed to scenario
    validation so a driver can be checked against the declared topology even
    when the environment itself had problems.
    """
    errors: List[str] = []
    service_names: List[str] = []

    data = _require_mapping(raw, errors, source, "<root>")
    if data is None:
        return errors, service_names

    if data.get("network") is not None:
        network = _require_string(data.get("network"), errors, source, "network")
        if network and not SERVICE_NAME_PATTERN.match(network):
            _err(
                errors, source, "network",
                "e2e.schema.invalid_service_name", name=network,
            )

    services = data.get("services")
    if services is None:
        _err(errors, source, "services", "e2e.schema.missing")
        return errors, service_names
    if not isinstance(services, list):
        _err(
            errors, source, "services",
            "e2e.schema.not_list", actual=_type_name(services),
        )
        return errors, service_names
    if not services:
        _err(errors, source, "services", "e2e.schema.no_services")
        return errors, service_names

    # Collected up front because a readiness probe may address a service
    # declared further down the list, and the reachability rule below has to
    # recognise the name wherever it appears.
    declared_names = {
        str(entry.get("name"))
        for entry in services
        if isinstance(entry, Mapping) and isinstance(entry.get("name"), str)
    }

    seen: set = set()
    for index, entry in enumerate(services):
        path = f"services[{index}]"
        service = _require_mapping(entry, errors, source, path)
        if service is None:
            continue

        name = _require_string(service.get("name"), errors, source, f"{path}.name")
        if name is not None:
            if not SERVICE_NAME_PATTERN.match(name):
                _err(
                    errors, source, f"{path}.name",
                    "e2e.schema.invalid_service_name", name=name,
                )
            elif name in seen:
                _err(
                    errors, source, f"{path}.name",
                    "e2e.schema.duplicate_service", name=name,
                )
            else:
                seen.add(name)
                service_names.append(name)

        _require_string(service.get("image"), errors, source, f"{path}.image")

        base_kind = service.get("base_kind", "base")
        _check_choice(base_kind, BASE_KINDS, errors, source, f"{path}.base_kind")

        _validate_build_steps(service.get("build"), errors, source, f"{path}.build")
        _validate_readiness(
            service.get("readiness"), errors, source, f"{path}.readiness",
            ports=service.get("ports"), declared_names=declared_names,
        )

        for list_field in ("ports", "command"):
            value = service.get(list_field)
            if value is None or isinstance(value, str):
                continue
            if not isinstance(value, list):
                _err(
                    errors, source, f"{path}.{list_field}",
                    "e2e.schema.not_list", actual=_type_name(value),
                )

        env = service.get("environment")
        if env is not None and not isinstance(env, Mapping):
            _err(
                errors, source, f"{path}.environment",
                "e2e.schema.not_mapping", actual=_type_name(env),
            )

        for str_field in ("workdir", "source_target"):
            if service.get(str_field) is not None:
                _require_string(
                    service.get(str_field), errors, source, f"{path}.{str_field}"
                )

    return errors, service_names


def service_base_kinds(raw: Any) -> Dict[str, str]:
    """Map each declared service name to its ``base_kind``.

    Read straight off the raw environment document (rather than off the typed
    declarations) so scenario validation can consult it in the same pass that
    validates the environment — including for a bundle the bootstrap step has
    generated but not yet written to disk.
    """
    kinds: Dict[str, str] = {}
    if not isinstance(raw, Mapping):
        return kinds
    services = raw.get("services")
    if not isinstance(services, list):
        return kinds
    for entry in services:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        kind = entry.get("base_kind", "base")
        if isinstance(name, str) and name and isinstance(kind, str):
            kinds.setdefault(name, kind)
    return kinds


def _validate_build_steps(
    value: Any, errors: List[str], source: str, path: str
) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        _err(errors, source, path, "e2e.schema.not_list", actual=_type_name(value))
        return
    for index, step in enumerate(value):
        _require_string(step, errors, source, f"{path}[{index}]")


def _validate_readiness(
    value: Any,
    errors: List[str],
    source: str,
    path: str,
    *,
    ports: Any = None,
    declared_names: Optional[Iterable[str]] = None,
) -> None:
    if value is None:
        return
    probe = _require_mapping(value, errors, source, path)
    if probe is None:
        return
    kind = probe.get("kind")
    if not _check_choice(kind, READINESS_KINDS, errors, source, f"{path}.kind"):
        return
    _check_required_fields(
        probe, _READINESS_FIELDS[kind], errors, source, path,
        context=t("e2e.schema.context.readiness", kind=kind),
    )
    _check_readiness_shapes(probe, errors, source, path)
    for budget in ("timeout", "interval"):
        if probe.get(budget) is not None:
            _require_positive(probe[budget], errors, source, f"{path}.{budget}")
    if kind == "http" and probe.get("status") is not None:
        _require_http_status(probe["status"], errors, source, f"{path}.status")
    if kind in ("http", "tcp"):
        _check_probe_reachability(
            probe, kind, errors, source, path, ports, declared_names or ()
        )


def _check_readiness_shapes(
    probe: Mapping[str, Any], errors: List[str], source: str, path: str
) -> None:
    """Type-check every readiness field the conversion layer will coerce."""
    for name, shape in _READINESS_FIELD_SHAPES.items():
        value = probe.get(name)
        if value is None:
            continue
        field_path = f"{path}.{name}"
        if shape == "string":
            _require_string(value, errors, source, field_path)
        elif shape == "port":
            _require_port(value, errors, source, field_path)
        elif shape == "argv":
            _require_argv(value, errors, source, field_path)


def _require_port(value: Any, errors: List[str], source: str, path: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        _err(errors, source, path, "e2e.schema.invalid_port", value=repr(value))


def _require_argv(value: Any, errors: List[str], source: str, path: str) -> None:
    """A command is either a shell string or a list of argv strings."""
    if isinstance(value, str):
        _require_string(value, errors, source, path)
        return
    if isinstance(value, list):
        if not value:
            _err(errors, source, path, "e2e.schema.empty")
            return
        for index, part in enumerate(value):
            _require_string(part, errors, source, f"{path}[{index}]")
        return
    _err(
        errors, source, path,
        "e2e.schema.not_string_or_list", actual=_type_name(value),
    )


def _require_http_status(value: Any, errors: List[str], source: str, path: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 100 <= value <= 599:
        _err(errors, source, path, "e2e.schema.invalid_status", value=repr(value))


def _published_host_ports(ports: Any) -> Optional[Set[int]]:
    """Host ports a service's ``ports`` mapping publishes.

    ``None`` means "cannot be determined" — a range, a bare container port
    (whose host side the runtime picks at random), or a shape this parser does
    not recognise. Callers skip the reachability check then rather than reject a
    mapping they simply failed to read.
    """
    if ports is None:
        return set()
    if isinstance(ports, str):
        ports = [ports]
    if not isinstance(ports, (list, tuple)):
        return None
    published: Set[int] = set()
    for entry in ports:
        text = str(entry).strip()
        if not text:
            continue
        text = text.split("/", 1)[0]
        parts = text.split(":")
        if len(parts) < 2:
            # `-p 8000` publishes to an ephemeral host port, so no fixed
            # address can be asserted about it.
            return None
        host_part = parts[-2]
        if not host_part.isdigit():
            return None
        published.add(int(host_part))
    return published


def _probe_address(probe: Mapping[str, Any], kind: str) -> Tuple[Optional[str], Optional[int]]:
    """The (host, port) an ``http``/``tcp`` probe dials, as readiness reads it."""
    url = probe.get("url")
    host: Optional[str] = None
    port: Optional[int] = None
    if isinstance(url, str) and url.strip():
        parsed = urlparse(url if "//" in url else "//" + url)
        host = parsed.hostname
        try:
            port = parsed.port
        except ValueError:
            port = None
        if port is None and parsed.scheme in ("http", "https"):
            port = 443 if parsed.scheme == "https" else 80
    if kind == "tcp":
        raw_port = probe.get("port")
        if isinstance(raw_port, int) and not isinstance(raw_port, bool):
            port = raw_port
        if host is None:
            # `_tcp_host` defaults to the host loopback when no url is given.
            host = "127.0.0.1"
    return host, port


def _check_probe_reachability(
    probe: Mapping[str, Any],
    kind: str,
    errors: List[str],
    source: str,
    path: str,
    ports: Any,
    declared_names: Iterable[str],
) -> None:
    """Reject an ``http``/``tcp`` probe the host process cannot actually reach.

    Two shapes fail, both of which look perfectly reasonable in a generated
    document: addressing a peer by its in-network service name (only the
    container network resolves that), and dialling a loopback port the service
    never published. Either one makes the probe poll until its budget runs out
    and then report a healthy service as an environment failure.
    """
    host, port = _probe_address(probe, kind)
    if not host:
        return
    lowered = host.lower()
    if lowered in {str(name).lower() for name in declared_names}:
        _err(
            errors, source, path, "e2e.schema.readiness_in_network_host",
            host=host, kind=kind,
        )
        return
    if lowered not in _LOOPBACK_HOSTS or port is None:
        return
    published = _published_host_ports(ports)
    if published is None or port in published:
        return
    _err(
        errors, source, path, "e2e.schema.readiness_port_not_published",
        port=port, kind=kind,
        published=", ".join(str(value) for value in sorted(published)) or "-",
    )


def validate_scenario(
    raw: Any,
    source: str,
    *,
    service_names: Iterable[str],
    environment_source: str,
    baselines_dir: Optional[Path] = None,
    require_existing_baselines: bool = True,
    service_kinds: Optional[Mapping[str, str]] = None,
) -> List[str]:
    """Validate one ``scenarios/*.yaml`` document against the declared services.

    ``service_kinds`` maps a declared service name to its ``base_kind`` and is
    what lets the driver be checked for *capability* rather than mere existence;
    omit it (as a caller validating a scenario in isolation does) and only the
    name check runs.
    """
    errors: List[str] = []
    known_services = list(service_names)

    data = _require_mapping(raw, errors, source, "<root>")
    if data is None:
        return errors

    _require_string(data.get("name"), errors, source, "name")

    driver = _require_string(data.get("driver"), errors, source, "driver")
    # WHY: an empty `known_services` means the environment failed to yield a
    # single usable service name, and its own errors already say why. Checking
    # drivers against nothing would then add one "unknown driver" line per
    # scenario and bury the actual diagnosis under the cascade.
    if driver is not None and known_services and driver not in known_services:
        _err(
            errors, source, "driver", "e2e.schema.unknown_driver",
            driver=driver, environment=environment_source,
        )
    _check_driver_capability(
        data, driver, errors, source, service_kinds, environment_source
    )

    if data.get("timeout") is not None:
        _require_positive(data["timeout"], errors, source, "timeout")

    visual_driving = _declared_flag(data, "visual_driving", errors, source)
    # Vetted like the ladder flags even though it unlocks nothing: the parse
    # layer reads every declaration flag by identity against `True`, so a
    # non-boolean `fail_fast: "true"` would otherwise validate cleanly and then
    # reach the executor as False — the author's explicit instruction discarded
    # with nothing on screen saying so.
    _declared_flag(data, "fail_fast", errors, source)
    _validate_actions(data.get("actions"), errors, source, visual_driving)
    _validate_assertions(
        data.get("assertions"), errors, source, baselines_dir,
        require_existing_baselines,
    )
    return errors


def _check_driver_capability(
    data: Mapping[str, Any],
    driver: Optional[str],
    errors: List[str],
    source: str,
    service_kinds: Optional[Mapping[str, str]],
    environment_source: str,
) -> None:
    """Reject a scenario whose driver cannot perform what the scenario declares.

    WHY at validation time rather than at execution: a ``browser`` action or a
    ``dom`` assertion needs a browser, which only the ``playwright`` base kind
    ships. The executor does raise on the mismatch — but only after the whole
    environment has been built and every readiness probe awaited, and the raise
    aborts the run, discarding the results of scenarios that already passed in
    the same round. The mismatch is knowable from the two documents alone, so it
    belongs where every other statically-decidable rule lives: before a single
    image is built, and before the bootstrap step writes the content out.
    """
    if driver is None or not service_kinds:
        return
    kind = service_kinds.get(driver)
    # An unknown driver, or one whose own `base_kind` is invalid, is already
    # reported where it belongs; deriving a second complaint from it would only
    # bury the first.
    if kind is None or kind not in BASE_KINDS or kind == PLAYWRIGHT_BASE_KIND:
        return

    actions = data.get("actions")
    if isinstance(actions, list):
        for index, entry in enumerate(actions):
            if (
                isinstance(entry, Mapping)
                and entry.get("action") in _BROWSER_ONLY_ACTIONS
            ):
                _err(
                    errors, source, f"actions[{index}]",
                    "e2e.schema.driver_not_browser",
                    driver=driver, base_kind=kind, environment=environment_source,
                )

    assertions = data.get("assertions")
    if isinstance(assertions, list):
        for index, entry in enumerate(assertions):
            if (
                isinstance(entry, Mapping)
                and entry.get("kind") in _BROWSER_ONLY_ASSERTIONS
            ):
                _err(
                    errors, source, f"assertions[{index}]",
                    "e2e.schema.driver_not_browser",
                    driver=driver, base_kind=kind, environment=environment_source,
                )


def _validate_actions(
    value: Any, errors: List[str], source: str, visual_driving: bool
) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        _err(
            errors, source, "actions",
            "e2e.schema.not_list", actual=_type_name(value),
        )
        return

    saw_browser = False
    for index, entry in enumerate(value):
        path = f"actions[{index}]"
        action = _require_mapping(entry, errors, source, path)
        if action is None:
            continue
        kind = action.get("action")
        if not _check_choice(kind, ACTION_KINDS, errors, source, f"{path}.action"):
            continue
        # INVARIANT: browser ops are batched into ONE Playwright program per
        # scenario (page state cannot survive `backend.exec`, which is one-shot),
        # and that program necessarily runs after the non-browser actions have
        # already executed. A non-browser action declared *after* a browser one
        # would therefore run BEFORE it — the declared order silently inverted,
        # so an `exec` meant to seed state for a loaded page would seed it before
        # the navigation. Rejecting the shape is honest; honouring it would mean
        # flushing the program early and losing the very page state the following
        # browser ops depend on.
        if kind == "browser":
            saw_browser = True
        elif saw_browser:
            _err(
                errors, source, path,
                "e2e.schema.action_after_browser", kind=str(kind),
            )
        _check_required_fields(
            action, _ACTION_FIELDS[kind], errors, source, path,
            context=t("e2e.schema.context.action", kind=kind),
        )
        # INVARIANT: the ladder applies to *driving* as well as asserting —
        # an interaction reachable through CLI / API / a DOM selector must not
        # be performed by clicking screen coordinates.
        if kind == "visual_click":
            if not visual_driving:
                _err(
                    errors, source, path,
                    "e2e.schema.ladder_visual_driving_undeclared",
                )
            if action.get("selector") is not None:
                _err(
                    errors, source, path,
                    "e2e.schema.ladder_driving_downgrade_available",
                )


def _validate_assertions(
    value: Any,
    errors: List[str],
    source: str,
    baselines_dir: Optional[Path],
    require_existing_baselines: bool = True,
) -> None:
    if value is None:
        _err(errors, source, "assertions", "e2e.schema.no_assertions")
        return
    if not isinstance(value, list):
        _err(
            errors, source, "assertions",
            "e2e.schema.not_list", actual=_type_name(value),
        )
        return
    if not value:
        _err(errors, source, "assertions", "e2e.schema.no_assertions")
        return

    for index, entry in enumerate(value):
        path = f"assertions[{index}]"
        assertion = _require_mapping(entry, errors, source, path)
        if assertion is None:
            continue
        _validate_one_assertion(
            assertion, errors, source, path, baselines_dir,
            require_existing_baselines,
        )


def _validate_one_assertion(
    assertion: Mapping[str, Any],
    errors: List[str],
    source: str,
    path: str,
    baselines_dir: Optional[Path],
    require_existing_baselines: bool = True,
) -> None:
    kind = assertion.get("kind")
    all_kinds = (
        tuple(DETERMINISTIC_ASSERTIONS)
        + tuple(VISUAL_REGRESSION_ASSERTIONS)
        + tuple(SEMANTIC_VISUAL_ASSERTIONS)
    )
    if not _check_choice(kind, all_kinds, errors, source, f"{path}.kind"):
        return

    declared_visual = _declared_flag(assertion, "visual_regression", errors, source, path)
    declared_semantic = _declared_flag(assertion, "semantic_visual", errors, source, path)

    if kind in DETERMINISTIC_ASSERTIONS:
        _check_required_fields(
            assertion, DETERMINISTIC_ASSERTIONS[kind], errors, source, path,
            context=t("e2e.schema.context.assertion", kind=kind),
        )
        # A tier flag on a tier-1 assertion is either a copy-paste slip or an
        # attempt to smuggle in escalation; either way it must not pass silently.
        for flag, declared in (
            ("visual_regression", declared_visual),
            ("semantic_visual", declared_semantic),
        ):
            if declared:
                _err(
                    errors, source, path,
                    "e2e.schema.ladder_flag_on_deterministic",
                    flag=flag, kind=kind,
                )
        return

    if kind in VISUAL_REGRESSION_ASSERTIONS:
        _check_required_fields(
            assertion, VISUAL_REGRESSION_ASSERTIONS[kind], errors, source, path,
            context=t("e2e.schema.context.assertion", kind=kind),
        )
        if not declared_visual:
            _err(
                errors, source, path,
                "e2e.schema.ladder_visual_regression_undeclared",
            )
        if declared_semantic:
            _err(
                errors, source, path,
                "e2e.schema.ladder_flag_on_deterministic",
                flag="semantic_visual", kind=kind,
            )
        _check_downgrade(
            assertion, _TIER2_DOWNGRADE_FIELDS, kind, errors, source, path
        )
        threshold = assertion.get("threshold")
        if threshold is not None:
            if (
                isinstance(threshold, bool)
                or not isinstance(threshold, (int, float))
                or not 0 <= threshold <= 1
            ):
                _err(
                    errors, source, f"{path}.threshold",
                    "e2e.schema.invalid_threshold", value=repr(threshold),
                )
        _check_baseline(
            assertion, errors, source, path, baselines_dir,
            require_existing_baselines,
        )
        return

    # Tier 3.
    _check_required_fields(
        assertion, SEMANTIC_VISUAL_ASSERTIONS[kind], errors, source, path,
        context=t("e2e.schema.context.assertion", kind=kind),
    )
    if not declared_semantic:
        _err(errors, source, path, "e2e.schema.ladder_semantic_visual_undeclared")
    if not _declared_flag(assertion, "require_evidence", errors, source, path):
        _err(errors, source, path, "e2e.schema.ladder_evidence_required")
    _check_downgrade(assertion, _TIER3_DOWNGRADE_FIELDS, kind, errors, source, path)


def _declared_flag(
    holder: Mapping[str, Any],
    name: str,
    errors: List[str],
    source: str,
    path: str = "",
) -> bool:
    """Read a ladder opt-in flag, demanding a real boolean.

    INVARIANT: the ladder rules say a tier is unlocked only by an explicit
    ``true``. Python truthiness would let the quoted string ``"false"`` — a
    plausible slip in a machine-written YAML document — read as a declaration,
    so the flag that is supposed to *record a deliberate escalation* would be
    satisfied by a value whose author meant the opposite. Anything that is not a
    bool is reported and counts as undeclared.
    """
    value = holder.get(name)
    if value is None:
        return False
    if not isinstance(value, bool):
        _err(
            errors, source, f"{path}.{name}" if path else name,
            "e2e.schema.not_bool", actual=_type_name(value),
        )
        return False
    return value


def _check_downgrade(
    assertion: Mapping[str, Any],
    fields: Sequence[str],
    kind: str,
    errors: List[str],
    source: str,
    path: str,
) -> None:
    """Flag an escalation whose own configuration proves a lower tier suffices."""
    for name in fields:
        if assertion.get(name) is not None:
            _err(
                errors, source, path,
                "e2e.schema.ladder_downgrade_available",
                kind=kind, field=name,
            )


def baseline_is_contained(baseline: str) -> bool:
    """Whether ``baseline`` names a file *inside* the baselines directory.

    INVARIANT: a baseline is a git-tracked asset of ``tianluo/e2e/baselines/``.
    Joining an absolute path onto that directory discards the directory outright
    (``Path("/x") / "/etc/hosts"`` is ``/etc/hosts``), and ``..`` walks out of
    it — so without this check a scenario could compare against, and
    ``--write-baselines`` could overwrite, an arbitrary file on the host while
    the "baseline lives in git" contract silently stopped holding.

    Shared with the runtime comparison in :mod:`tianluo.e2e.assertions`, so a
    declaration built in code (never validated here) is rejected too.
    """
    text = str(baseline or "").strip()
    if not text:
        return False
    # Both flavours are checked: a document written on one platform is read on
    # whichever platform runs the flow, and "C:/x" must not become admissible
    # just because the reader is POSIX.
    #
    # WHY anchoring is tested directly instead of trusting is_absolute(): a
    # Windows path counts as absolute only when it carries *both* a drive and a
    # root, so the rooted-but-driveless "\evil.png" and the drive-relative
    # "C:evil.png" both report False — yet joining either onto the baselines
    # directory throws that directory away (the first keeps the drive and lands
    # at its root, the second re-anchors on the drive's own cwd). What has to be
    # rejected is any name that carries its own anchor, drive or root.
    windows = PureWindowsPath(text)
    if text.startswith(("/", "\\")) or windows.drive or windows.root:
        return False
    parts = PurePosixPath(text.replace("\\", "/")).parts
    return bool(parts) and ".." not in parts


def _check_baseline(
    assertion: Mapping[str, Any],
    errors: List[str],
    source: str,
    path: str,
    baselines_dir: Optional[Path],
    require_existing: bool = True,
) -> None:
    """Existence check for a tier-2 baseline image.

    The only IO this module performs, and only when the caller supplies a
    directory — so validating an in-memory document (bootstrap, tests) stays a
    pure function.

    WHY ``require_existing`` exists: a baseline can only ever be produced by
    *running* the scenario inside the image that renders it, so the very first
    run of a new tier-2 assertion necessarily starts with no file on disk. If
    absence were unconditionally fatal, content loading — the first thing both
    the E2E step and ``luo e2e run`` do — would abort before the assertion layer
    could capture that first shot, making ``luo e2e run --write-baselines``
    unreachable and a flow-authored visual-regression scenario impossible.
    Callers that have explicitly asked for first capture (or are validating a
    just-generated document) pass ``False``; everyone else keeps the strict rule,
    so an ordinary run still reports a vanished baseline as a locatable
    configuration problem instead of silently degrading to "no comparison".
    """
    baseline = assertion.get("baseline")
    if baseline is None:
        # Absence is already reported as a missing required field; saying it
        # twice would only pad the report.
        return
    # A non-string baseline must not slip through as "check skipped": the value
    # goes on to be joined onto a path at run time, and a validator that stays
    # silent here would hand the assertion layer something it never vetted.
    if not isinstance(baseline, str):
        _err(
            errors, source, f"{path}.baseline",
            "e2e.schema.not_string", actual=_type_name(baseline),
        )
        return
    if not baseline.strip():
        _err(errors, source, f"{path}.baseline", "e2e.schema.empty")
        return
    if not baseline_is_contained(baseline):
        _err(
            errors, source, f"{path}.baseline",
            "e2e.schema.baseline_escapes", baseline=baseline,
        )
        return
    if not require_existing or baselines_dir is None:
        return
    if not (Path(baselines_dir) / baseline).is_file():
        _err(
            errors, source, f"{path}.baseline",
            "e2e.schema.baseline_missing",
            baseline=baseline, directory=str(baselines_dir),
        )


def validate_content(
    raw: Any,
    source: str,
    *,
    baselines_dir: Optional[Path] = None,
    require_existing_baselines: bool = True,
) -> List[str]:
    """Validate a whole ``tianluo/e2e/`` content bundle.

    ``raw`` is the bundle :func:`tianluo.e2e.content_config.read_raw_content`
    produces::

        {
            "environment": <parsed environment.yaml>,
            "environment_source": "tianluo/e2e/environment.yaml",
            "scenarios": {"tianluo/e2e/scenarios/login.yaml": <parsed>, ...},
        }

    ``source`` labels the directory itself and is used for problems that belong
    to no single file. Pass ``baselines_dir`` to additionally check that every
    tier-2 baseline image exists; omit it to keep the call pure. Pass
    ``require_existing_baselines=False`` when a not-yet-captured baseline is
    legitimate (first capture, or a freshly generated document) — see
    :func:`_check_baseline`.
    """
    errors: List[str] = []

    bundle = _require_mapping(raw, errors, source, "<content>")
    if bundle is None:
        return errors

    env_source = str(bundle.get("environment_source") or source)
    env_errors, service_names = validate_environment(
        bundle.get("environment"), env_source
    )
    errors.extend(env_errors)
    service_kinds = service_base_kinds(bundle.get("environment"))

    scenarios = bundle.get("scenarios")
    if not isinstance(scenarios, Mapping):
        _err(
            errors, source, "scenarios",
            "e2e.schema.not_mapping", actual=_type_name(scenarios),
        )
        return errors

    seen_names: Dict[str, str] = {}
    for scenario_source, document in scenarios.items():
        errors.extend(
            validate_scenario(
                document,
                str(scenario_source),
                service_names=service_names,
                environment_source=env_source,
                baselines_dir=baselines_dir,
                require_existing_baselines=require_existing_baselines,
                service_kinds=service_kinds,
            )
        )
        # Scenario names are the selection keys `e2e.scenarios` matches on, so a
        # duplicate would make selection silently ambiguous.
        if isinstance(document, Mapping):
            name = document.get("name")
            if isinstance(name, str) and name:
                if name in seen_names:
                    _err(
                        errors, str(scenario_source), "name",
                        "e2e.schema.duplicate_scenario",
                        name=name, other=seen_names[name],
                    )
                else:
                    seen_names[name] = str(scenario_source)

    return errors
