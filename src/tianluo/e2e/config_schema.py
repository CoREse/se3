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
caller passes no ``baselines_dir``). No third-party validation library either —
the rules below are specific enough that a jsonschema document would be both
larger and unable to express the ladder checks.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from tianluo.i18n import t

__all__ = [
    "ACTION_KINDS",
    "BASE_KINDS",
    "DETERMINISTIC_ASSERTIONS",
    "READINESS_KINDS",
    "SEMANTIC_VISUAL_ASSERTIONS",
    "SERVICE_NAME_PATTERN",
    "VISUAL_REGRESSION_ASSERTIONS",
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

READINESS_KINDS = ("command", "http", "tcp", "log")

# kind -> (always-required fields, at-least-one-of fields)
_READINESS_FIELDS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "command": (("command",), ()),
    "http": (("url",), ()),
    "tcp": (("port",), ()),
    "log": (("pattern",), ()),
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
    if isinstance(value, bool) or not isinstance(value, (int, float)):
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
            service.get("readiness"), errors, source, f"{path}.readiness"
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
    value: Any, errors: List[str], source: str, path: str
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
    for budget in ("timeout", "interval"):
        if probe.get(budget) is not None:
            _require_positive(probe[budget], errors, source, f"{path}.{budget}")


def validate_scenario(
    raw: Any,
    source: str,
    *,
    service_names: Iterable[str],
    environment_source: str,
    baselines_dir: Optional[Path] = None,
) -> List[str]:
    """Validate one ``scenarios/*.yaml`` document against the declared services."""
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

    if data.get("timeout") is not None:
        _require_positive(data["timeout"], errors, source, "timeout")

    visual_driving = bool(data.get("visual_driving", False))
    _validate_actions(data.get("actions"), errors, source, visual_driving)
    _validate_assertions(data.get("assertions"), errors, source, baselines_dir)
    return errors


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

    for index, entry in enumerate(value):
        path = f"actions[{index}]"
        action = _require_mapping(entry, errors, source, path)
        if action is None:
            continue
        kind = action.get("action")
        if not _check_choice(kind, ACTION_KINDS, errors, source, f"{path}.action"):
            continue
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
        _validate_one_assertion(assertion, errors, source, path, baselines_dir)


def _validate_one_assertion(
    assertion: Mapping[str, Any],
    errors: List[str],
    source: str,
    path: str,
    baselines_dir: Optional[Path],
) -> None:
    kind = assertion.get("kind")
    all_kinds = (
        tuple(DETERMINISTIC_ASSERTIONS)
        + tuple(VISUAL_REGRESSION_ASSERTIONS)
        + tuple(SEMANTIC_VISUAL_ASSERTIONS)
    )
    if not _check_choice(kind, all_kinds, errors, source, f"{path}.kind"):
        return

    declared_visual = bool(assertion.get("visual_regression", False))
    declared_semantic = bool(assertion.get("semantic_visual", False))

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
        _check_baseline(assertion, errors, source, path, baselines_dir)
        return

    # Tier 3.
    _check_required_fields(
        assertion, SEMANTIC_VISUAL_ASSERTIONS[kind], errors, source, path,
        context=t("e2e.schema.context.assertion", kind=kind),
    )
    if not declared_semantic:
        _err(errors, source, path, "e2e.schema.ladder_semantic_visual_undeclared")
    if not assertion.get("require_evidence", False):
        _err(errors, source, path, "e2e.schema.ladder_evidence_required")
    _check_downgrade(assertion, _TIER3_DOWNGRADE_FIELDS, kind, errors, source, path)


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


def _check_baseline(
    assertion: Mapping[str, Any],
    errors: List[str],
    source: str,
    path: str,
    baselines_dir: Optional[Path],
) -> None:
    """Existence check for a tier-2 baseline image.

    The only IO this module performs, and only when the caller supplies a
    directory — so validating an in-memory document (bootstrap, tests) stays a
    pure function.
    """
    baseline = assertion.get("baseline")
    if baselines_dir is None or not isinstance(baseline, str) or not baseline:
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
    tier-2 baseline image exists; omit it to keep the call pure.
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
