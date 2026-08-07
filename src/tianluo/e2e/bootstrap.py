"""tianluo.e2e.bootstrap — authoring and evolving the ``tianluo/e2e/`` content.

The e2e configuration is split across two files with two different owners:

* ``tianluo.yaml``'s ``e2e:`` block — *runtime settings*, owned by the **user**.
  ``enabled`` there is the user's promise that Docker or Podman is installed and
  that the fix loop may spend time running scenarios.
* ``tianluo/e2e/`` — *content*: the services topology, the environment build
  steps, the scenario definitions and the baseline screenshots. Owned by the
  **flow**, which authors it on first use and evolves it thereafter, exactly the
  way it authors and evolves test code.

INVARIANT: this module never writes ``tianluo.yaml``. Not to flip ``enabled``,
not to add a scenario name, not for anything. Enabling e2e is a commitment about
the *machine* and about how the fix loop spends its time, and a program cannot
make that commitment on a person's behalf. :func:`suggest_enable` is the entire
extent of what the flow may do about a project that looks like a good fit — it
returns a sentence, and writes nothing. Every write in this module is funnelled
through :func:`_resolve_target`, which refuses any path that resolves outside
the content directory, so the guarantee is mechanical rather than a convention.

Two entry points, deliberately asymmetric in how they fail:

:func:`ensure_content`
    First-time generation. Raises :class:`~tianluo.e2e.errors.E2EConfigError`
    when it cannot produce admissible content — with no content at all there is
    nothing to run, so the E2E step must land on FAILED and open the ordinary
    human-escalation channel rather than pretend to pass.

:func:`evolve_content`
    Incremental evolution of content that already exists and already validates.
    A failure here *degrades*: the existing suite is still perfectly runnable, so
    a bad evolution proposal is discarded (reported in the result) instead of
    breaking a run that would otherwise have succeeded.

Both validate a candidate document set *in memory* before a single byte reaches
disk, so a rejected generation leaves no half-written directory behind.
"""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

from tianluo.i18n import t

from .config_schema import (
    ACTION_KINDS,
    BASE_KINDS,
    DETERMINISTIC_ASSERTIONS,
    READINESS_KINDS,
    SEMANTIC_VISUAL_ASSERTIONS,
    VISUAL_REGRESSION_ASSERTIONS,
    validate_content,
)
from .content_config import (
    ENVIRONMENT_FILENAME,
    SCENARIOS_DIR_NAME,
    baselines_dir,
    content_dir,
    content_relpath,
    read_raw_content,
)
from .errors import E2EConfigError, E2EContentIncompleteError

logger = logging.getLogger(__name__)

__all__ = [
    "BootstrapResult",
    "ensure_content",
    "evolve_content",
    "suggest_enable",
]

# One generation attempt plus one retry that is shown the validator's complaints.
# WHY exactly one retry: a schema violation the model cannot fix when told
# precisely what is wrong is a sign the request itself is unworkable, and the
# project has a human-escalation channel for that. Looping further just burns
# calls on the same misunderstanding.
MAX_GENERATION_ATTEMPTS = 2

# Scenario file names come from the model, so they are constrained rather than
# trusted: one path component, a YAML suffix, nothing that can traverse upward.
_SCENARIO_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.(yaml|yml)$")

_HEADER = """\
# Maintained by tianluo's e2e step. Hand edits are preserved: incremental
# evolution merges only the keys it means to change and never rewrites this
# file wholesale. Comments, however, do not survive an automated edit — put
# durable notes in a `description:` field instead of in a comment.
"""


@dataclass
class BootstrapResult:
    """What one bootstrap/evolution pass did.

    ``created`` is read by the E2E step handler to decide whether to mention the
    generation in its summary, so it means "content that did not exist now
    does", not merely "some file was written".
    """

    created: bool = False
    evolved: bool = False
    written: Tuple[str, ...] = ()
    skipped: Tuple[str, ...] = ()
    errors: Tuple[str, ...] = ()
    note: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.written)

    @property
    def ok(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------------------
# public entry points
# ---------------------------------------------------------------------------


def ensure_content(
    project_root: Path,
    flow: Any = None,
    *,
    caller: Any = None,
) -> BootstrapResult:
    """Make sure ``tianluo/e2e/`` holds a runnable environment and scenarios.

    Returns immediately when the directory is already complete — the flow does
    not re-derive content it (or a human) already authored. When part of it is
    missing, only the missing part is generated: an existing ``environment.yaml``
    is fed to the model as context and left untouched on disk.

    Raises :class:`~tianluo.e2e.errors.E2EConfigError` when admissible content
    could not be produced, and when content that is already on disk cannot be
    read. Nothing is written in either case.
    """
    root = Path(project_root)
    directory = str(content_relpath(root))

    # INVARIANT: a document that exists but cannot be used is an error here, not
    # an absence. Reading it tolerantly (empty file -> {}) made the two halves of
    # generation disagree — the prompt would ask for a complete environment while
    # `apply` kept the empty one — so both LLM calls were spent producing answers
    # that were then discarded, and the guaranteed failure arrived at the end.
    # Failing now costs no call and names the offending file. Overwriting it is
    # not an option either: unparsable YAML may still hold hand-written content.
    env_path = content_dir(root) / ENVIRONMENT_FILENAME
    environment_doc: Optional[Dict[str, Any]] = (
        _read_document(env_path, str(content_relpath(root, ENVIRONMENT_FILENAME)))
        if env_path.is_file()
        else None
    )
    existing_scenarios = _existing_scenarios(root)
    if environment_doc is not None and existing_scenarios:
        logger.debug("e2e content already present under %s", directory)
        return BootstrapResult()

    has_environment = environment_doc is not None

    proposal = _generate(
        root,
        flow=flow,
        caller=caller,
        existing_environment=environment_doc,
        existing_scenarios=existing_scenarios,
    )

    written = _write_documents(
        root,
        environment=None if has_environment else proposal["environment"],
        scenarios=proposal["scenarios"],
        overwrite=False,
    )
    _seed_baselines_dir(root)

    logger.info("e2e content generated under %s: %s", directory, ", ".join(written))
    return BootstrapResult(
        created=True,
        written=tuple(written),
        note=t("e2e.bootstrap.created", directory=directory),
    )


def evolve_content(
    project_root: Path,
    flow: Any = None,
    hints: Optional[Sequence[str]] = None,
    *,
    caller: Any = None,
) -> BootstrapResult:
    """Incrementally evolve existing ``tianluo/e2e/`` content.

    INVARIANT: evolution is expressed as *operations on keys* — ``set`` replaces
    named top-level keys, ``append`` extends named list keys — never as a
    replacement document. A whole-file rewrite is how a person's hand-tuned
    readiness probe, extra service or carefully narrowed assertion silently
    disappears; keeping the model's output key-scoped means anything it does not
    name survives untouched. For the same reason an ``add`` aimed at a file that
    already exists is refused rather than applied, and nothing here ever deletes
    a scenario.

    A proposal that fails validation is discarded and reported in
    :attr:`BootstrapResult.errors`: the content already on disk is valid and
    runnable, so a bad suggestion must not break the run.
    """
    root = Path(project_root)
    directory = str(content_relpath(root))

    try:
        raw = read_raw_content(root)
    except E2EContentIncompleteError:
        # A structurally half-present directory (an environment with no
        # scenarios, or the reverse) is a failed earlier bootstrap, not something
        # to evolve — completing it is what generation does. WHY only this
        # subclass: catching every E2EConfigError swallowed a corrupted scenario
        # file too, and generation's own "already complete" check would then find
        # the broken file present and report "unchanged" with exit 0 — hiding, at
        # the exact command the user ran to maintain the content, a directory
        # every other e2e command rejects.
        raw = None
    if raw is None:
        # Nothing to evolve yet — the honest response to "evolve" on an
        # un-bootstrapped project is to bootstrap it.
        return ensure_content(root, flow, caller=caller)

    environment = _as_mapping(raw.get("environment"))
    scenarios: Dict[str, Dict[str, Any]] = {}
    for source, document in (raw.get("scenarios") or {}).items():
        scenarios[Path(str(source)).name] = _as_mapping(document)

    try:
        proposal = _call_model(
            root,
            flow=flow,
            caller=caller,
            prompt_builder=lambda feedback: _evolve_prompt(
                root, flow, hints, environment, scenarios, feedback
            ),
            required_keys=["scenarios"],
            schema_hint=_EVOLVE_SCHEMA_HINT,
            apply=lambda data: _apply_evolution(environment, scenarios, data),
            baselines=baselines_dir(root),
            directory=directory,
        )
    except E2EConfigError as exc:
        logger.warning("e2e content evolution rejected: %s", exc)
        return BootstrapResult(errors=(str(exc),), note=str(exc))

    changed_environment = (
        proposal["environment"] if proposal["environment"] != environment else None
    )
    changed_scenarios = {
        name: document
        for name, document in proposal["scenarios"].items()
        if document != scenarios.get(name)
    }
    if not changed_environment and not changed_scenarios:
        return BootstrapResult(
            skipped=tuple(proposal["skipped"]),
            note=t("e2e.bootstrap.unchanged", directory=directory),
        )

    written = _write_documents(
        root,
        environment=changed_environment,
        scenarios=changed_scenarios,
        overwrite=True,
    )
    return BootstrapResult(
        evolved=True,
        written=tuple(written),
        skipped=tuple(proposal["skipped"]),
        note=t(
            "e2e.bootstrap.evolved",
            directory=directory,
            scenarios=len(changed_scenarios),
        ),
    )


def suggest_enable(project_root: Path) -> str:
    """Return a suggestion to turn e2e on, or ``""`` when there is nothing to say.

    INVARIANT: text only. This function opens no file for writing and returns no
    handle to one — the ``e2e.enabled`` switch stays the user's to flip, because
    turning it on asserts something about their machine (an unprivileged Docker
    or Podman) and about how much time the fix loop may spend. The caller prints
    the sentence; nobody edits ``tianluo.yaml``.
    """
    root = Path(project_root)

    from tianluo.config import E2EConfig

    if E2EConfig.load(root).enabled:
        return ""
    if content_dir(root).is_dir():
        # Content already authored: whoever did that knows e2e exists, so the
        # suggestion would be noise.
        return ""
    if not any((root / marker).exists() for marker in _FIT_MARKERS):
        return ""
    return t("e2e.bootstrap.suggest_enable")


# A project shaped like something that can be started, built or packaged is a
# project e2e can drive. Deliberately file-existence only: this runs on every
# flow that has e2e off, so it must stay far cheaper than the suggestion is
# valuable.
_FIT_MARKERS = (
    "pyproject.toml",
    "setup.py",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "Dockerfile",
    "docker-compose.yml",
    "compose.yaml",
    "Makefile",
)


# ---------------------------------------------------------------------------
# generation
# ---------------------------------------------------------------------------


def _generate(
    root: Path,
    *,
    flow: Any,
    caller: Any,
    existing_environment: Optional[Dict[str, Any]],
    existing_scenarios: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Produce a validated ``{environment, scenarios}`` pair, or raise."""
    directory = str(content_relpath(root))

    def apply(data: Mapping[str, Any]) -> Dict[str, Any]:
        environment = existing_environment
        if environment is None:
            environment = _as_mapping(data.get("environment"))
            if not environment:
                raise E2EConfigError(
                    t(
                        "e2e.bootstrap.failed",
                        directory=directory,
                        detail=t("e2e.bootstrap.no_response"),
                    )
                )
        scenarios = dict(existing_scenarios)
        produced = _scenario_entries(data)
        if not produced and not existing_scenarios:
            raise E2EConfigError(
                t(
                    "e2e.bootstrap.failed",
                    directory=directory,
                    detail=t("e2e.bootstrap.no_response"),
                )
            )
        for name, document in produced.items():
            scenarios[name] = document
        return {
            "environment": environment,
            "scenarios": scenarios,
            "skipped": [],
        }

    proposal = _call_model(
        root,
        flow=flow,
        caller=caller,
        prompt_builder=lambda feedback: _generate_prompt(
            root, flow, existing_environment, existing_scenarios, feedback
        ),
        required_keys=["scenarios"],
        schema_hint=_GENERATE_SCHEMA_HINT,
        apply=apply,
        baselines=baselines_dir(root),
        directory=directory,
    )
    # Only the newly produced scenarios are handed back for writing; the ones
    # already on disk took part in validation but must not be rewritten.
    proposal["scenarios"] = {
        name: document
        for name, document in proposal["scenarios"].items()
        if name not in existing_scenarios
    }
    return proposal


def _call_model(
    root: Path,
    *,
    flow: Any,
    caller: Any,
    prompt_builder,
    required_keys: Sequence[str],
    schema_hint: str,
    apply,
    baselines: Path,
    directory: str,
) -> Dict[str, Any]:
    """Ask the model, apply its answer, validate the result — up to N attempts.

    The candidate is validated with the *same* :func:`validate_content` the
    loader runs, so anything this accepts is by construction loadable afterwards.
    Validation happens entirely in memory: the caller only writes once a
    candidate has been accepted.

    WHY the baseline-existence rule is relaxed here: a scenario being authored
    for the first time cannot have its baseline screenshot yet — the image can
    only be produced by running the scenario inside the container that renders
    it. Requiring the file at generation time would make it impossible for the
    flow to ever author a visual-regression scenario, no matter how clearly the
    subject under test is a visual rendering. The capture step
    (``luo e2e run --write-baselines``, reviewed then committed by a human) is
    what closes the gap, and every other baseline rule still applies.
    """
    live_caller = caller if caller is not None else _build_caller(root, flow)
    feedback = ""
    last_errors: List[str] = []

    for attempt in range(MAX_GENERATION_ATTEMPTS):
        data = _ask(live_caller, prompt_builder(feedback), required_keys, schema_hint)
        if data is None:
            last_errors = [t("e2e.bootstrap.no_response")]
            feedback = t("e2e.bootstrap.no_response")
            continue

        try:
            candidate = apply(data)
        except E2EConfigError as exc:
            # A structurally unusable answer (no environment where one is
            # required, no scenario at all) is the same kind of problem as a
            # schema violation, so it feeds the same retry rather than aborting
            # the attempt budget early.
            last_errors = [str(exc)]
            feedback = str(exc)
            continue

        errors = validate_content(
            _bundle(root, candidate["environment"], candidate["scenarios"]),
            directory,
            baselines_dir=baselines,
            require_existing_baselines=False,
        )
        if not errors:
            return candidate

        last_errors = errors
        logger.info(
            "e2e content proposal rejected on attempt %d/%d: %s",
            attempt + 1,
            MAX_GENERATION_ATTEMPTS,
            "; ".join(errors[:5]),
        )
        feedback = "\n".join("- " + message for message in errors)

    raise E2EConfigError(
        t(
            "e2e.bootstrap.failed",
            directory=directory,
            detail="\n".join("- " + message for message in last_errors),
        )
    )


def _ask(
    caller: Any,
    prompt: str,
    required_keys: Sequence[str],
    schema_hint: str,
) -> Optional[Dict[str, Any]]:
    """One LLM round trip, returning the parsed object or ``None``.

    Every failure mode — a transport error, an unparsable answer, a non-object
    payload — collapses to ``None`` so the retry loop above owns the whole
    policy in one place.
    """
    # WHY inside the function body: the charter's core-dependency isolation. This
    # module is reachable from the E2E step handler, which a core-only install
    # imports; pulling the engine's LLM stack (and its JSON tooling) in at module
    # scope would put it on that path for every project, e2e or not.
    from ..engine.utils.json_parser import parse_json_response

    try:
        response = caller.call(
            prompt=prompt,
            json_mode="two_phase",
            json_schema_hint=schema_hint,
            required_keys=list(required_keys),
        )
    except Exception as exc:  # noqa: BLE001 - any caller failure is one retry
        logger.warning("e2e content generation call failed: %s", exc)
        return None

    try:
        data = parse_json_response(response, required_keys=list(required_keys))
    except Exception as exc:  # noqa: BLE001 - parser failures are answers too
        logger.warning("e2e content generation response was unparsable: %s", exc)
        return None
    if not isinstance(data, Mapping):
        return None
    return dict(data)


def _build_caller(root: Path, flow: Any) -> Any:
    """Construct the engine LLM caller for a bootstrap round.

    ``force_read_only=True``: the model only *proposes* documents — this module's
    Python decides what lands on disk and where. Without the lock the agent could
    write the files itself, bypassing both the schema validation and the
    content-directory containment guard that make this module safe.
    """
    from ..engine.llm_caller import LLMCaller

    return LLMCaller(
        root,
        flow_id=getattr(flow, "flow_id", None),
        step_id="e2e_bootstrap",
        step_type="e2e",
        force_read_only=True,
    )


# ---------------------------------------------------------------------------
# applying an evolution proposal
# ---------------------------------------------------------------------------


def _apply_evolution(
    environment: Dict[str, Any],
    scenarios: Dict[str, Dict[str, Any]],
    data: Mapping[str, Any],
) -> Dict[str, Any]:
    """Fold the model's key-scoped operations into copies of the live documents."""
    skipped: List[str] = []

    new_environment = _apply_ops(environment, _as_mapping(data.get("environment")))
    new_scenarios: Dict[str, Dict[str, Any]] = {
        name: copy.deepcopy(document) for name, document in scenarios.items()
    }

    for entry in _iter_mappings(data.get("scenarios")):
        name = _scenario_filename(entry.get("file"))
        if name is None:
            skipped.append(str(entry.get("file")))
            continue
        operation = str(entry.get("operation") or "update").strip().lower()
        exists = name in new_scenarios

        if operation == "add":
            if exists:
                # Refusing rather than merging: an `add` naming an existing file
                # means the model believes the file is not there, so its document
                # was written without knowledge of what it would replace.
                skipped.append(name)
                continue
            document = _as_mapping(entry.get("document"))
            if not document:
                skipped.append(name)
                continue
            document.setdefault("name", Path(name).stem)
            new_scenarios[name] = document
            continue

        if not exists:
            skipped.append(name)
            continue
        new_scenarios[name] = _apply_ops(new_scenarios[name], entry)

    return {
        "environment": new_environment,
        "scenarios": new_scenarios,
        "skipped": skipped,
    }


def _apply_ops(document: Mapping[str, Any], ops: Mapping[str, Any]) -> Dict[str, Any]:
    """Apply ``set`` / ``append`` operations to a copy of ``document``.

    ``append`` skips items already present so replaying an evolution — which
    happens whenever a fix loop revisits the same step — does not accumulate
    duplicate assertions round after round.
    """
    result: Dict[str, Any] = copy.deepcopy(dict(document))
    for key, value in _as_mapping(ops.get("set")).items():
        result[str(key)] = value
    for key, value in _as_mapping(ops.get("append")).items():
        current = result.get(str(key))
        if current is None:
            merged: List[Any] = []
        elif isinstance(current, list):
            merged = list(current)
        else:
            merged = [current]
        for item in value if isinstance(value, list) else [value]:
            if item not in merged:
                merged.append(item)
        result[str(key)] = merged
    return result


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


def _resolve_target(root: Path, relative: str) -> Path:
    """Resolve one write target, refusing anything outside ``tianluo/e2e/``.

    INVARIANT: every write in this module goes through here. The file names come
    from an LLM, so ``../../tianluo.yaml`` is a shape the input can genuinely
    take — containment has to be checked, not assumed.
    """
    base = content_dir(root).resolve()
    target = (content_dir(root) / relative).resolve()
    if target != base and base not in target.parents:
        raise E2EConfigError(
            t("e2e.bootstrap.path_outside", file=relative, directory=str(base))
        )
    return target


def _write_documents(
    root: Path,
    *,
    environment: Optional[Mapping[str, Any]],
    scenarios: Mapping[str, Mapping[str, Any]],
    overwrite: bool,
) -> List[str]:
    """Serialize the accepted documents, returning the repo-relative paths."""
    written: List[str] = []

    targets: List[Tuple[str, Mapping[str, Any]]] = []
    if environment:
        targets.append((ENVIRONMENT_FILENAME, environment))
    for name in sorted(scenarios):
        targets.append(("{}/{}".format(SCENARIOS_DIR_NAME, name), scenarios[name]))

    # Resolve every target before writing any of them: a containment violation on
    # the third file must not leave the first two on disk.
    resolved = [(relative, _resolve_target(root, relative), doc) for relative, doc in targets]

    for relative, path, document in resolved:
        if path.exists() and not overwrite:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_dump(document), encoding="utf-8")
        written.append(str(content_relpath(root, *relative.split("/"))))
    return written


def _dump(document: Mapping[str, Any]) -> str:
    return _HEADER + yaml.safe_dump(
        dict(document), sort_keys=False, allow_unicode=True, default_flow_style=False
    )


def _seed_baselines_dir(root: Path) -> None:
    """Create ``baselines/`` so ``--write-baselines`` has somewhere to land.

    Kept with a ``.gitkeep`` because the directory is a git-tracked asset location
    and an empty directory does not survive a clone.
    """
    marker = _resolve_target(root, "baselines/.gitkeep")
    marker.parent.mkdir(parents=True, exist_ok=True)
    if not marker.exists():
        marker.write_text("", encoding="utf-8")


# ---------------------------------------------------------------------------
# reading what is already there
# ---------------------------------------------------------------------------


def _existing_scenarios(root: Path) -> Dict[str, Dict[str, Any]]:
    """Parse the scenario files already on disk, keyed by file name."""
    directory = content_dir(root) / SCENARIOS_DIR_NAME
    if not directory.is_dir():
        return {}
    found: Dict[str, Dict[str, Any]] = {}
    for path in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
        found[path.name] = _read_document(
            path, str(content_relpath(root, SCENARIOS_DIR_NAME, path.name))
        )
    return found


def _read_document(path: Path, label: str) -> Dict[str, Any]:
    """Parse one on-disk content document, refusing an unusable one.

    WHY strict rather than degrading to ``{}``: an empty or unparsable file
    counted as "present" for the completeness check and as "absent" for the
    prompt, so generation would either burn its whole call budget on answers it
    discarded or report "nothing to do" for a directory the loader rejects. The
    file is never rewritten — malformed YAML can still hold work a person typed —
    so the honest outcome is a located error telling them which file to fix.
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise E2EConfigError(
            t("e2e.bootstrap.unreadable_document", file=label, detail=str(exc))
        ) from exc
    if not isinstance(data, Mapping) or not data:
        raise E2EConfigError(t("e2e.bootstrap.unusable_document", file=label))
    return dict(data)


def _as_mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _iter_mappings(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(entry) for entry in value if isinstance(entry, Mapping)]


def _scenario_filename(value: Any) -> Optional[str]:
    """Normalize a model-supplied scenario file name, or ``None`` if unusable."""
    if not isinstance(value, str) or not value.strip():
        return None
    name = value.strip()
    if not name.endswith((".yaml", ".yml")):
        name += ".yaml"
    if not _SCENARIO_FILENAME.match(name):
        return None
    return name


def _scenario_entries(data: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Extract ``{filename: document}`` from a generation response."""
    produced: Dict[str, Dict[str, Any]] = {}
    for entry in _iter_mappings(data.get("scenarios")):
        name = _scenario_filename(entry.get("file") or entry.get("name"))
        document = _as_mapping(entry.get("document")) or _as_mapping(entry)
        if name is None or not document:
            continue
        # A bare document (no `document:` wrapper) still carries `file`, which is
        # a bookkeeping key rather than part of the scenario schema.
        document.pop("file", None)
        document.pop("operation", None)
        document.setdefault("name", Path(name).stem)
        produced[name] = document
    return produced


def _bundle(
    root: Path,
    environment: Mapping[str, Any],
    scenarios: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    """Assemble the raw bundle shape :func:`validate_content` expects.

    Built with the same repo-relative labels
    :func:`~tianluo.e2e.content_config.read_raw_content` uses, so a validation
    message points at the path the file will actually have once written.
    """
    return {
        "environment": dict(environment),
        "environment_source": str(content_relpath(root, ENVIRONMENT_FILENAME)),
        "scenarios": {
            str(content_relpath(root, SCENARIOS_DIR_NAME, name)): dict(document)
            for name, document in scenarios.items()
        },
        "source": str(content_relpath(root)),
    }


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------
#
# Not localized: like every other prompt payload in the engine these are written
# for the implementing agent, not for a human reading the console.

_GENERATE_SCHEMA_HINT = (
    '{"environment": {"network": "tianluo-e2e", "services": [...]}, '
    '"scenarios": [{"file": "cli-smoke.yaml", "document": {"name": "cli-smoke", '
    '"driver": "app", "actions": [...], "assertions": [...]}}], "notes": "..."}'
)

_EVOLVE_SCHEMA_HINT = (
    '{"environment": {"set": {}, "append": {}}, '
    '"scenarios": [{"file": "cli-smoke.yaml", "operation": "update", '
    '"set": {"timeout": 120}, "append": {"assertions": [...]}}], "notes": "..."}'
)

_LADDER_RULES = """\
ASSERTION LADDER — a hard rule, enforced by the schema validator, not advice:
- Use the LOWEST tier that can express the check. Escalating past a tier that
  would have worked is a validation error, because it converts deterministic
  verification into probabilistic verification.
- Tier 1 (default, no declaration): exit_code, stdout, stderr, http_status,
  http_body, file_exists, file_content, dom. Prefer these always. A rendered web
  page has a DOM, so assert on the DOM, not on pixels.
- Tier 2 (screenshot_diff) requires `visual_regression: true` on the assertion
  and a baseline image committed under tianluo/e2e/baselines/. Only when the
  subject under test genuinely IS a visual rendering. Do NOT emit tier-2
  assertions during first generation: the baseline does not exist yet and the
  configuration would be rejected.
- Tier 3 (visual_semantic) requires BOTH `semantic_visual: true` and
  `require_evidence: true`. Last resort, for GUIs with no queryable structure.
- The same rule governs driving: `visual_click` needs `visual_driving: true` at
  scenario level and is only for GUIs with no programmatic entry point.

DRIVER CAPABILITY — `browser` actions and `dom` assertions need a browser, so the
scenario's driver must be a service with `base_kind: playwright`. Declare one (the
official Playwright image) and point the scenario at it; a `base` driver with a
`dom` assertion is a validation error.

ACTION ORDER — `browser` operations are batched into ONE Playwright program that
runs after every other action of the scenario, so a non-browser action declared
AFTER a browser one is a validation error: put all exec / http / wait / screenshot
actions first, then the browser sequence. To capture a page image inside a browser
scenario use a browser `op: screenshot` (the `screenshot` action captures a virtual
X display and belongs to a gui-xvfb driver).

READINESS PROBES — where the probe runs decides what it can address:
- `command` probes run INSIDE the container, so they see the shared network and
  may address peers by service name (curl http://app:8000/health, pg_isready -h
  db). This is the default choice.
- `http` and `tcp` probes are dialled FROM THE HOST, so they only reach a port
  the service publishes: `ports: ["18000:8000"]` together with
  `url: http://127.0.0.1:18000/health`. An `http`/`tcp` probe aimed at a service
  name, or at an unpublished localhost port, is a validation error.
- An `http` probe accepts 2xx/3xx by default; add `status: <code>` when the
  service answers something else while healthy.
"""


def _vocabulary() -> str:
    """The admissible vocabulary, derived from the schema module itself.

    WHY derived rather than written out: the prompt and the validator would
    otherwise drift, and the drift is invisible — the model would confidently
    emit a kind the validator rejects, and every generation would burn its retry.
    """
    return "\n".join(
        [
            "services[].base_kind: " + ", ".join(BASE_KINDS),
            "services[].readiness.kind: " + ", ".join(READINESS_KINDS),
            "scenario actions[].action: " + ", ".join(ACTION_KINDS),
            "tier-1 assertion kinds: " + ", ".join(sorted(DETERMINISTIC_ASSERTIONS)),
            "tier-2 assertion kinds: " + ", ".join(sorted(VISUAL_REGRESSION_ASSERTIONS)),
            "tier-3 assertion kinds: " + ", ".join(sorted(SEMANTIC_VISUAL_ASSERTIONS)),
        ]
    )


def _generate_prompt(
    root: Path,
    flow: Any,
    existing_environment: Optional[Mapping[str, Any]],
    existing_scenarios: Mapping[str, Mapping[str, Any]],
    feedback: str,
) -> str:
    parts = [
        "You are authoring the e2e content configuration for a project managed "
        "by tianluo. It is declarative YAML — the execution framework itself "
        "lives in tianluo and must NOT be reproduced in the project.",
        "",
        "Produce a container topology that can boot this project from scratch, "
        "plus a small first set of scenarios that exercise its primary user-"
        "visible behaviour end to end. Favour few, meaningful scenarios over "
        "many shallow ones: every scenario costs container time on every fix "
        "iteration.",
        "",
        "## Project context",
        _project_context(root, flow),
        "",
        "## Reference: environment.yaml",
        "```yaml",
        _read_asset("environment.example.yaml"),
        "```",
        "",
        "## Reference: one scenario file",
        "```yaml",
        _read_asset("scenario.example.yaml"),
        "```",
        "",
        "## Admissible vocabulary",
        _vocabulary(),
        "",
        _LADDER_RULES,
    ]

    if existing_environment:
        parts += [
            "",
            "## environment.yaml already exists — do NOT regenerate it",
            "Write scenarios against the services it declares:",
            "```yaml",
            yaml.safe_dump(dict(existing_environment), sort_keys=False),
            "```",
        ]
    if existing_scenarios:
        parts += [
            "",
            "## Scenarios already present (do not duplicate their coverage)",
            ", ".join(sorted(existing_scenarios)),
        ]

    parts += [
        "",
        "## Output",
        "Return ONE JSON object:",
        _GENERATE_SCHEMA_HINT,
        "`environment` is the complete environment.yaml document"
        + (" (omit it: one already exists)" if existing_environment else "")
        + ". Each `scenarios[]` entry gives a file name (one path component, "
        "`.yaml` suffix) and the complete scenario document. Every scenario's "
        "`driver` must name a service declared in `environment`, and it must be "
        "able to do what the scenario declares: a `browser` action or a `dom` "
        "assertion needs a service whose `base_kind` is `playwright`.",
    ]

    if feedback:
        parts += [
            "",
            "## Your previous answer was rejected by the schema validator",
            "Fix exactly these problems and return the corrected JSON:",
            feedback,
        ]
    return "\n".join(parts)


def _evolve_prompt(
    root: Path,
    flow: Any,
    hints: Optional[Sequence[str]],
    environment: Mapping[str, Any],
    scenarios: Mapping[str, Mapping[str, Any]],
    feedback: str,
) -> str:
    parts = [
        "You are evolving an existing e2e content configuration for a project "
        "managed by tianluo. Work INCREMENTALLY: propose only the keys that must "
        "change. Anything you do not name is preserved, and a person may have "
        "hand-tuned it — never restate a whole document to 'clean it up'.",
        "",
        "## Project context",
        _project_context(root, flow),
        "",
        "## Current environment.yaml",
        "```yaml",
        yaml.safe_dump(dict(environment), sort_keys=False),
        "```",
        "",
        "## Current scenarios",
    ]
    for name in sorted(scenarios):
        parts += [
            "### {}".format(name),
            "```yaml",
            yaml.safe_dump(dict(scenarios[name]), sort_keys=False),
            "```",
        ]

    if hints:
        parts += [
            "",
            "## What changed in this task (cover it with e2e)",
            "\n".join("- " + str(hint) for hint in hints if str(hint).strip()),
        ]

    parts += [
        "",
        "## Admissible vocabulary",
        _vocabulary(),
        "",
        _LADDER_RULES,
        "",
        "## Output",
        "Return ONE JSON object:",
        _EVOLVE_SCHEMA_HINT,
        "Operations: `operation: \"add\"` with a complete `document` for a NEW "
        "file (the name must not already exist); `operation: \"update\"` with "
        "`set` (replace these top-level keys) and/or `append` (extend these list "
        "keys) for an existing file. There is no delete operation. Return an "
        "empty `scenarios` list when the existing suite already covers the "
        "change.",
    ]

    if feedback:
        parts += [
            "",
            "## Your previous answer was rejected by the schema validator",
            "Fix exactly these problems and return the corrected JSON:",
            feedback,
        ]
    return "\n".join(parts)


# How much of one manifest is worth showing. Enough to read the dependency list
# and the entry points, far short of vendoring the file into the prompt.
_MANIFEST_LIMIT = 4000
_CHARTER_LIMIT = 4000

_MANIFESTS = (
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "Dockerfile",
    "docker-compose.yml",
    "compose.yaml",
    "Makefile",
)


def _project_context(root: Path, flow: Any) -> str:
    """Assemble the tech-stack context the model needs to pick base images."""
    parts: List[str] = ["Project root: {}".format(root.name or str(root))]

    task = str(getattr(flow, "task_description", "") or "").strip()
    if task:
        parts += ["", "Current task: " + task]

    entries = _top_level_entries(root)
    if entries:
        parts += ["", "Top-level entries: " + ", ".join(entries)]

    for manifest in _MANIFESTS:
        path = root / manifest
        if not path.is_file():
            continue
        text = _read_text(path, _MANIFEST_LIMIT)
        if text:
            parts += ["", "### {}".format(manifest), "```", text, "```"]

    charter = root / _runtime_charter_relpath(root)
    if charter.is_file():
        text = _read_text(charter, _CHARTER_LIMIT)
        if text:
            parts += ["", "### project charter (excerpt)", text]

    return "\n".join(parts)


def _runtime_charter_relpath(root: Path) -> Path:
    from tianluo.runtime_paths import runtime_relpath

    return runtime_relpath(root, "charter.md")


def _top_level_entries(root: Path, limit: int = 40) -> List[str]:
    try:
        names = sorted(
            entry.name + ("/" if entry.is_dir() else "")
            for entry in root.iterdir()
            if not entry.name.startswith(".")
        )
    except OSError:
        return []
    return names[:limit]


def _read_text(path: Path, limit: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) > limit:
        text = text[:limit] + "\n… (truncated)"
    return text


def _read_asset(name: str) -> str:
    """Read one shipped example document from the package's templates directory.

    Resolved through ``importlib.resources`` for the same reason the Dockerfile
    templates are (see :mod:`tianluo.e2e.templates`): these are package data, and
    a wheel and a source checkout must take the same path.
    """
    try:
        from importlib.resources import files as _files

        return _files("tianluo.e2e").joinpath("templates", name).read_text(
            encoding="utf-8"
        )
    except Exception as exc:  # pragma: no cover - interpreter-dependent
        logger.debug("importlib.resources could not resolve %s: %s", name, exc)

    candidate = Path(__file__).resolve().parent / "templates" / name
    if candidate.is_file():
        return candidate.read_text(encoding="utf-8")
    return ""
