"""tianluo.e2e.content_config — the project-side ``tianluo/e2e/`` content layer.

Where :class:`tianluo.config.E2EConfig` carries the *runtime* settings the user
owns, this module loads the *content* the flow authors and evolves:

``<runtime>/e2e/``
    ``environment.yaml``   services topology + declarative build steps
    ``scenarios/*.yaml``   one scenario per file: driver, actions, assertions
    ``baselines/``         git-tracked baseline screenshots for tier-2 diffs

Parsing is deliberately split from validation: this module turns YAML into typed
declarations and hands the raw documents to
:func:`tianluo.e2e.config_schema.validate_content` for the rule checks, so the
schema stays a pure function that a caller can run against a document it has not
written to disk yet (the bootstrap step does exactly that).

stdlib + PyYAML only — PyYAML is already a core dependency, so this module is
importable without the ``tianluo[e2e]`` extra.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

import yaml

from tianluo.runtime_paths import runtime_dir, runtime_relpath

from .backend import BindMount, EnvironmentSpec, ReadinessProbe, ServiceSpec
from .errors import E2EConfigError

logger = logging.getLogger(__name__)

__all__ = [
    "AssertionDecl",
    "ActionDecl",
    "CONTENT_DIR_NAME",
    "E2EContent",
    "ENVIRONMENT_FILENAME",
    "ScenarioDecl",
    "ServiceDecl",
    "baselines_dir",
    "content_dir",
    "content_relpath",
    "load_content_config",
    "read_raw_content",
]

CONTENT_DIR_NAME = "e2e"
ENVIRONMENT_FILENAME = "environment.yaml"
SCENARIOS_DIR_NAME = "scenarios"
BASELINES_DIR_NAME = "baselines"

# Where the project's source tree lands inside every service container. A fixed
# default keeps generated scenarios portable; a service may override it.
DEFAULT_SOURCE_TARGET = "/workspace"

# Default network name suffix; the session prefixes it with the flow id so two
# concurrent worktree runs never collide on one network.
DEFAULT_NETWORK = "tianluo-e2e"


def content_dir(project_root: Path) -> Path:
    """Absolute path of the project's e2e content directory.

    Routed through :func:`tianluo.runtime_paths.runtime_dir` rather than a
    hardcoded ``tianluo/`` so a checkout still on the legacy ``se3/`` layout
    resolves to its own directory during the 12.x transition.
    """
    return runtime_dir(project_root) / CONTENT_DIR_NAME


def content_relpath(project_root: Path, *parts: str) -> Path:
    """Repo-relative path inside the e2e content directory, for messages/git."""
    return runtime_relpath(project_root, CONTENT_DIR_NAME, *parts)


def baselines_dir(project_root: Path) -> Path:
    """Directory holding git-tracked baseline screenshots."""
    return content_dir(project_root) / BASELINES_DIR_NAME


@dataclass(frozen=True)
class ServiceDecl:
    """One declared container in the environment topology.

    ``base_kind`` picks the Dockerfile template family — the three base-image
    sources the design distinguishes: ``base`` (a generic public image for
    CLI / web / API services), ``playwright`` (the official browser image,
    which also pins font rendering so visual baselines reproduce), and
    ``gui-xvfb`` (tianluo's own Xvfb + window-manager + screenshot recipe for
    desktop GUI apps).
    """

    name: str
    image: str
    base_kind: str = "base"
    build: Tuple[str, ...] = ()
    readiness: Optional[Mapping[str, Any]] = None
    ports: Tuple[str, ...] = ()
    environment: Mapping[str, str] = field(default_factory=dict)
    command: Tuple[str, ...] = ()
    workdir: Optional[str] = None
    # External dependencies (postgres, redis) get no source mount: they run a
    # stock public image and must not see the project's tree.
    mount_source: bool = True
    source_target: str = DEFAULT_SOURCE_TARGET

    @property
    def pulls_image_as_is(self) -> bool:
        """Whether this service uses its base image unmodified.

        A generic base with no build steps needs no local image at all — that is
        the ``postgres:16`` case, and skipping the build keeps environment
        creation fast.
        """
        return self.base_kind == "base" and not self.build

    def to_spec(self, project_root: Path) -> ServiceSpec:
        """Convert to the backend-facing :class:`ServiceSpec`."""
        mounts: Tuple[BindMount, ...] = ()
        if self.mount_source:
            mounts = (
                BindMount(source=Path(project_root), target=self.source_target),
            )
        readiness = _readiness_to_probe(self.readiness)
        return ServiceSpec(
            name=self.name,
            base_image=self.image,
            template=None if self.pulls_image_as_is else self.base_kind,
            build_steps=tuple(self.build),
            readiness=readiness,
            ports=tuple(self.ports),
            environment=dict(self.environment),
            mounts=mounts,
            command=tuple(self.command),
            workdir=self.workdir or (self.source_target if self.mount_source else None),
        )


@dataclass(frozen=True)
class ActionDecl:
    """One step of a scenario's operation sequence.

    ``kind`` names the driving mechanism (``exec``, ``http``, ``browser``,
    ``wait``, ``screenshot``, ``visual_click``); ``params`` holds the rest of
    the mapping verbatim so the executor reads what it needs without this layer
    having to know every action's parameter set.
    """

    kind: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)


@dataclass(frozen=True)
class AssertionDecl:
    """One assertion, carrying its tier declaration.

    ``visual_regression`` / ``semantic_visual`` are not conveniences — they are
    the explicit opt-ins the assertion ladder demands for tiers 2 and 3, and
    :mod:`tianluo.e2e.config_schema` refuses a document where a higher-tier
    assertion appears without its flag.
    """

    kind: str
    params: Mapping[str, Any] = field(default_factory=dict)
    visual_regression: bool = False
    semantic_visual: bool = False
    require_evidence: bool = False

    def get(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)


@dataclass(frozen=True)
class ScenarioDecl:
    """One test scenario: where it runs, what it does, what must hold."""

    name: str
    driver: str
    source: str
    description: str = ""
    actions: Tuple[ActionDecl, ...] = ()
    assertions: Tuple[AssertionDecl, ...] = ()
    timeout: Optional[int] = None
    tags: Tuple[str, ...] = ()
    visual_driving: bool = False


@dataclass(frozen=True)
class E2EContent:
    """The whole parsed ``tianluo/e2e/`` directory."""

    project_root: Path
    root: Path
    network: str
    services: Tuple[ServiceDecl, ...]
    scenarios: Tuple[ScenarioDecl, ...]
    baselines: Path

    def service(self, name: str) -> Optional[ServiceDecl]:
        for svc in self.services:
            if svc.name == name:
                return svc
        return None

    def scenario(self, name: str) -> Optional[ScenarioDecl]:
        for scenario in self.scenarios:
            if scenario.name == name:
                return scenario
        return None

    def to_environment_spec(
        self, *, network: Optional[str] = None, labels: Optional[Mapping[str, str]] = None
    ) -> EnvironmentSpec:
        """Convert to the backend-facing :class:`EnvironmentSpec`."""
        return EnvironmentSpec(
            project_root=self.project_root,
            network=network or self.network,
            services=tuple(svc.to_spec(self.project_root) for svc in self.services),
            labels=dict(labels or {}),
        )


def _readiness_to_probe(raw: Optional[Mapping[str, Any]]) -> Optional[ReadinessProbe]:
    """Build a :class:`ReadinessProbe` from a validated readiness mapping."""
    if not raw:
        return None
    command = raw.get("command") or ()
    if isinstance(command, str):
        command = (command,)
    probe_kwargs: Dict[str, Any] = {
        "kind": str(raw.get("kind", "command")),
        "command": tuple(str(part) for part in command),
        "url": raw.get("url"),
        "pattern": raw.get("pattern"),
    }
    port = raw.get("port")
    probe_kwargs["port"] = int(port) if port is not None else None
    if raw.get("timeout") is not None:
        probe_kwargs["timeout"] = float(raw["timeout"])
    if raw.get("interval") is not None:
        probe_kwargs["interval"] = float(raw["interval"])
    return ReadinessProbe(**probe_kwargs)


def _read_yaml(path: Path, label: str) -> Any:
    """Parse one YAML document, converting any failure into an E2EConfigError.

    Unlike the tolerant project-config reader, a malformed e2e content file is
    fatal: these files are flow-generated, so silently degrading to defaults
    would run a *different* e2e suite than the one that was authored, and the
    resulting pass would be meaningless.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise E2EConfigError(f"{label}: cannot be read: {exc}") from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise E2EConfigError(f"{label}: is not valid YAML: {exc}") from exc


def read_raw_content(project_root: Path) -> Optional[Dict[str, Any]]:
    """Read the content directory into the raw bundle the schema validates.

    Returns ``None`` when the directory does not exist at all — the
    "not bootstrapped yet" signal. Raises :class:`E2EConfigError` when the
    directory exists but is structurally unusable (half-present: an
    ``environment.yaml`` with no scenarios, or scenarios with no environment),
    because that state is a broken bootstrap rather than an absent one.
    """
    root = content_dir(project_root)
    if not root.is_dir():
        return None

    env_path = root / ENVIRONMENT_FILENAME
    scenarios_root = root / SCENARIOS_DIR_NAME
    scenario_paths: list = []
    if scenarios_root.is_dir():
        for pattern in ("*.yaml", "*.yml"):
            scenario_paths.extend(scenarios_root.glob(pattern))
    # Sorted so the reported error order is stable across filesystems.
    scenario_paths = sorted(set(scenario_paths))

    dir_label = str(content_relpath(project_root))
    env_label = str(content_relpath(project_root, ENVIRONMENT_FILENAME))

    if not env_path.is_file():
        if scenario_paths:
            raise E2EConfigError(
                f"{dir_label}: scenarios are declared but {ENVIRONMENT_FILENAME} "
                f"is missing; the services topology they run against is undefined"
            )
        # Directory present but empty — indistinguishable from "not bootstrapped"
        # for the caller's purposes, so report the same sentinel.
        return None

    if not scenario_paths:
        raise E2EConfigError(
            f"{dir_label}: {ENVIRONMENT_FILENAME} is present but "
            f"{SCENARIOS_DIR_NAME}/ declares no scenario; e2e would build an "
            f"environment and assert nothing"
        )

    scenarios: Dict[str, Any] = {}
    for path in scenario_paths:
        label = str(content_relpath(project_root, SCENARIOS_DIR_NAME, path.name))
        data = _read_yaml(path, label)
        if isinstance(data, dict) and not data.get("name"):
            data = dict(data)
            data["name"] = path.stem
        scenarios[label] = data

    return {
        "environment": _read_yaml(env_path, env_label),
        "environment_source": env_label,
        "scenarios": scenarios,
        "source": dir_label,
    }


def load_content_config(project_root: Path) -> Optional[E2EContent]:
    """Load and validate ``tianluo/e2e/`` into typed declarations.

    Returns ``None`` when the directory has not been bootstrapped yet, so the
    caller can trigger first-time generation instead of treating an absent
    directory as an error. Raises :class:`E2EConfigError` when the content is
    present but invalid.
    """
    raw = read_raw_content(project_root)
    if raw is None:
        return None

    # Imported here rather than at module scope purely to keep the parse layer
    # and the rule layer independently importable; both are stdlib-only.
    from . import config_schema

    errors = config_schema.validate_content(
        raw, raw["source"], baselines_dir=baselines_dir(project_root)
    )
    if errors:
        raise E2EConfigError(
            "e2e configuration is invalid:\n"
            + "\n".join(f"  - {message}" for message in errors)
        )

    env_data = raw["environment"] or {}
    services = tuple(
        _build_service(entry) for entry in (env_data.get("services") or [])
    )
    scenarios = tuple(
        _build_scenario(data, source)
        for source, data in raw["scenarios"].items()
    )
    return E2EContent(
        project_root=Path(project_root),
        root=content_dir(project_root),
        network=str(env_data.get("network") or DEFAULT_NETWORK),
        services=services,
        scenarios=scenarios,
        baselines=baselines_dir(project_root),
    )


def _as_tuple(value: Any) -> Tuple[str, ...]:
    """Normalise a scalar-or-list YAML value to a tuple of strings."""
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return (str(value),)


def _build_service(entry: Mapping[str, Any]) -> ServiceDecl:
    return ServiceDecl(
        name=str(entry["name"]),
        image=str(entry["image"]),
        base_kind=str(entry.get("base_kind", "base")),
        build=_as_tuple(entry.get("build")),
        readiness=dict(entry["readiness"]) if entry.get("readiness") else None,
        ports=_as_tuple(entry.get("ports")),
        environment={
            str(key): str(value) for key, value in (entry.get("environment") or {}).items()
        },
        command=_as_tuple(entry.get("command")),
        workdir=str(entry["workdir"]) if entry.get("workdir") else None,
        mount_source=bool(entry.get("mount_source", True)),
        source_target=str(entry.get("source_target") or DEFAULT_SOURCE_TARGET),
    )


def _build_scenario(data: Mapping[str, Any], source: str) -> ScenarioDecl:
    actions = tuple(
        ActionDecl(
            kind=str(entry.get("action")),
            params={k: v for k, v in entry.items() if k != "action"},
        )
        for entry in (data.get("actions") or [])
    )
    assertions = tuple(
        AssertionDecl(
            kind=str(entry.get("kind")),
            params={
                k: v
                for k, v in entry.items()
                if k not in ("kind", "visual_regression", "semantic_visual",
                             "require_evidence")
            },
            visual_regression=bool(entry.get("visual_regression", False)),
            semantic_visual=bool(entry.get("semantic_visual", False)),
            require_evidence=bool(entry.get("require_evidence", False)),
        )
        for entry in (data.get("assertions") or [])
    )
    raw_timeout = data.get("timeout")
    return ScenarioDecl(
        name=str(data.get("name")),
        driver=str(data.get("driver")),
        source=source,
        description=str(data.get("description") or ""),
        actions=actions,
        assertions=assertions,
        timeout=int(raw_timeout) if raw_timeout is not None else None,
        tags=_as_tuple(data.get("tags")),
        visual_driving=bool(data.get("visual_driving", False)),
    )
