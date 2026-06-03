"""SPEC_GATE step handler — mechanism A.

Closes the "spec edited by ``update_spec`` but never re-tested" gap. The default
``feature`` / ``discovery`` step sequence runs
``... → update_spec → SPEC_GATE → version_analyze → commit``, so this gate sits
between the (non-read-only) ``update_spec`` edit and the commit.

The gate is a pure program step (``uses_llm=False``). It has two phases:

1. **Artifact check (no LLM, no test parsing).** For every spec the flow edited
   or newly created since flow start, run
   :func:`spec_validator.validate_spec_structure` and — for *edited* specs only —
   verify the requirement count / name set did not shrink relative to the
   pre-``update_spec`` snapshot. A structural failure, an unparseable spec, or a
   deleted requirement is an *invalid artifact* → route back to ``update_spec``.

2. **Full re-test.** When the artifact is clean, re-run the entire test suite
   through the shared :func:`steps.test.run_and_classify_tests` core (same
   command, phases, dynamic timeout, critical gate, baseline split as the real
   ``test`` step). An introduced (non-baseline) failure means the spec edit broke
   a spec-content test → route to ``implement`` (the existing fix loop). A clean
   run completes the gate.

When the flow changed no spec at all the gate is a no-op (``COMPLETED``).

The stable pre-``update_spec`` snapshot lives in
``flow.state.context['spec_requirement_baseline']`` and is captured once by the
state machine before it first dispatches ``UPDATE_SPEC``. :func:`build_spec_requirement_baseline`
is the canonical builder of that snapshot, exported here so the state machine and
the handler share one format (single source of truth).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ...utils import discover_specs, parse_spec
from ..models import FlowInstance, Step, StepStatus
from ..spec_validator import validate_spec_structure

logger = logging.getLogger(__name__)

# Snapshot dict keys (kept as module constants so the builder and the handler
# never drift on the field names).
_SNAP_CONTENT = "content"
_SNAP_REQUIREMENTS = "requirements"

_REQUIREMENT_RE = re.compile(r"^###+\s+Requirement:", re.MULTILINE)


def _spec_name_from_path(spec_path: str) -> str:
    """Return the spec name (its directory name) for a ``.../<name>/spec.md`` path."""
    return Path(spec_path).parent.name


def _requirement_names_from_content(content: str) -> List[str]:
    """Extract the ordered list of requirement names from spec.md ``content``.

    Mirrors :func:`utils.parse_spec`'s requirement detection (``### Requirement:``
    headings) so a snapshot built from in-memory content and a current parse via
    ``parse_spec`` agree on the name set.
    """
    names: List[str] = []
    for line in content.splitlines():
        if _REQUIREMENT_RE.match(line):
            name = line.split(":", 1)[1].strip() if ":" in line else ""
            names.append(name)
    return names


def build_spec_requirement_baseline(project_root: Path) -> Dict[str, Dict[str, Any]]:
    """Capture a stable snapshot of every on-disk spec at flow start.

    Returns ``{spec_name: {"content": <full spec.md text>,
    "requirements": [<requirement names>]}}``. Used by the state machine to
    populate ``flow.state.context['spec_requirement_baseline']`` once, before the
    first ``UPDATE_SPEC`` dispatch. The SPEC_GATE handler diffs the current disk
    state against this snapshot to find edited / new specs and to enforce the
    requirement non-decrease invariant on edited specs.

    The snapshot is intentionally captured a single time per flow (not re-taken
    before each ``update_spec`` redo): re-snapshotting after a bad edit landed on
    disk would let the gate measure non-decrease against an already-corrupted
    baseline and wave the deletion through.
    """
    snapshot: Dict[str, Dict[str, Any]] = {}
    for spec_path in discover_specs(str(project_root)):
        try:
            content = Path(spec_path).read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("Could not read spec %s for baseline snapshot: %s", spec_path, exc)
            continue
        name = _spec_name_from_path(spec_path)
        snapshot[name] = {
            _SNAP_CONTENT: content,
            _SNAP_REQUIREMENTS: _requirement_names_from_content(content),
        }
    return snapshot


def _read_current_specs(project_root: Path) -> Dict[str, Tuple[str, str]]:
    """Return ``{spec_name: (filepath, content)}`` for every current on-disk spec."""
    current: Dict[str, Tuple[str, str]] = {}
    for spec_path in discover_specs(str(project_root)):
        try:
            content = Path(spec_path).read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("Could not read spec %s during spec_gate: %s", spec_path, exc)
            continue
        current[_spec_name_from_path(spec_path)] = (spec_path, content)
    return current


def _classify_changed_specs(
    snapshot: Dict[str, Dict[str, Any]],
    current: Dict[str, Tuple[str, str]],
) -> Tuple[List[str], List[str]]:
    """Split current specs into (edited, new) relative to the snapshot.

    A spec is *new* when it is absent from the snapshot (created by update_spec),
    and *edited* when present in the snapshot but with different content. Deleted
    specs are out of scope for this gate (the spec guardrails forbid requirement
    deletion at a finer grain and the merge/commit guardrails cover file removal).
    """
    edited: List[str] = []
    new: List[str] = []
    for name, (_path, content) in sorted(current.items()):
        snap = snapshot.get(name)
        if snap is None:
            new.append(name)
        elif content != snap.get(_SNAP_CONTENT):
            edited.append(name)
    return edited, new


def _check_artifacts(
    edited: List[str],
    new: List[str],
    snapshot: Dict[str, Dict[str, Any]],
    current: Dict[str, Tuple[str, str]],
) -> List[str]:
    """Run the (non-LLM) artifact checks; return a list of human-readable errors.

    For every edited or new spec the content MUST pass
    ``validate_spec_structure``. For *edited* specs only, the requirement count
    and name set MUST NOT shrink relative to the snapshot (new specs have no
    prior baseline, so only the structural check applies to them).
    """
    errors: List[str] = []

    for name in [*edited, *new]:
        spec_path, content = current[name]
        result = validate_spec_structure(content, name)
        if not result.passed:
            joined = "; ".join(result.errors)
            errors.append(f"spec '{name}' failed structural validation: {joined}")

    for name in edited:
        spec_path, _content = current[name]
        snap_names = list(snapshot.get(name, {}).get(_SNAP_REQUIREMENTS, []))
        try:
            current_reqs = parse_spec(spec_path).get("requirements", [])
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(f"spec '{name}' could not be parsed: {exc}")
            continue
        current_names = [r.get("title", "") for r in current_reqs]

        if len(current_names) < len(snap_names):
            errors.append(
                f"spec '{name}' lost requirements: count dropped from "
                f"{len(snap_names)} to {len(current_names)}"
            )
        removed = [n for n in snap_names if n not in set(current_names)]
        if removed:
            errors.append(
                f"spec '{name}' removed requirement(s): {', '.join(removed)}"
            )

    return errors


def spec_gate_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the SPEC_GATE step (mechanism A).

    Returns:
        ``StepStatus.COMPLETED`` when no spec changed or the artifact is clean and
        the full re-test passes; ``StepStatus.REVISION_NEEDED`` (with
        ``gate_route`` set to ``update_spec`` or ``implement``) otherwise.
    """
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    snapshot = flow.state.context.get("spec_requirement_baseline")
    if not isinstance(snapshot, dict):
        # The pre-update_spec snapshot was never captured (e.g. a sequence
        # without update_spec, or an interrupted/legacy flow). Without it the
        # gate cannot tell edited specs from new ones, so it cannot make a
        # trustworthy routing decision — skip rather than mis-route.
        logger.warning(
            "spec_gate: no 'spec_requirement_baseline' snapshot in context; "
            "skipping the gate (cannot determine edited specs)."
        )
        step.outputs["gate_passed"] = True
        step.outputs["gate_route"] = ""
        step.outputs["fix_needed"] = False
        return StepStatus.COMPLETED

    current = _read_current_specs(project_root)
    edited, new = _classify_changed_specs(snapshot, current)

    if not edited and not new:
        logger.info("spec_gate: no spec changes detected — gate skipped (no-op).")
        step.outputs["gate_passed"] = True
        step.outputs["gate_route"] = ""
        step.outputs["gate_skipped"] = True
        step.outputs["fix_needed"] = False
        return StepStatus.COMPLETED

    logger.info(
        "spec_gate: checking %d edited spec(s) and %d new spec(s): edited=%s new=%s",
        len(edited), len(new), edited, new,
    )

    # ------------------------------------------------------------------
    # Phase 1: programmatic artifact check (no LLM, no test parsing).
    # ------------------------------------------------------------------
    artifact_errors = _check_artifacts(edited, new, snapshot, current)
    if artifact_errors:
        instructions = _build_artifact_fix_instructions(artifact_errors)
        logger.warning("spec_gate: invalid spec artifact, routing back to update_spec:\n%s", instructions)
        step.outputs["gate_passed"] = False
        step.outputs["gate_route"] = "update_spec"
        step.outputs["fix_needed"] = True
        step.outputs["fix_instructions"] = instructions
        step.outputs["fix_context"] = {
            "reason": "spec_gate_artifact_invalid",
            "gate_route": "update_spec",
            "spec_errors": artifact_errors,
            "edited_specs": edited,
            "new_specs": new,
        }
        return StepStatus.REVISION_NEEDED

    # ------------------------------------------------------------------
    # Phase 2: full re-test through the shared test core.
    # ------------------------------------------------------------------
    from ...config import TestConfig
    from .test import run_and_classify_tests

    config = TestConfig.load(project_root)
    verdict = run_and_classify_tests(
        project_root=project_root,
        flow=flow,
        tests_added=step.inputs.get("tests_added", []),
        baseline_failures=step.inputs.get("baseline_failures") or [],
        # Run the FULL suite at the gate (not a fix-iteration subset): a spec
        # edit can break any spec-content test, including phases marked
        # in_fix_loop=false.
        is_fix_iteration=False,
        fix_iteration=flow.state.get_fix_iteration(),
        estimated_test_duration=step.inputs.get("estimated_test_duration"),
        config=config,
    )

    step.outputs["test_results"] = verdict.test_results

    if verdict.should_fix:
        # An introduced (non-baseline) failure — or an in-budget baseline
        # failure under mechanism B — survived the spec edit. Code-first: the
        # fix is to update the implementation / the stale test, NEVER to revert
        # a legitimate spec change. Route to implement (the existing fix loop),
        # which can edit code and tests.
        logger.warning(
            "spec_gate: full re-test blocking after spec edit, routing to implement."
        )
        step.outputs["gate_passed"] = False
        step.outputs["gate_route"] = "implement"
        step.outputs["fix_needed"] = True
        step.outputs["fix_instructions"] = verdict.fix_instructions
        fix_context = dict(verdict.fix_context)
        fix_context["gate_route"] = "implement"
        fix_context.setdefault("reason", "spec_gate_test_failure")
        step.outputs["fix_context"] = fix_context
        return StepStatus.REVISION_NEEDED

    logger.info("spec_gate: spec artifact clean and full re-test passed — gate cleared.")
    step.outputs["gate_passed"] = True
    step.outputs["gate_route"] = ""
    step.outputs["fix_needed"] = False
    return StepStatus.COMPLETED


def _build_artifact_fix_instructions(errors: List[str]) -> str:
    """Build the fix_instructions for an invalid-artifact route back to update_spec."""
    bullet = "\n".join(f"  - {e}" for e in errors)
    return (
        "The spec edit(s) made by update_spec produced an INVALID artifact and "
        "must be redone. The following structural / requirement problems were "
        "found:\n"
        f"{bullet}\n\n"
        "Fix each spec so it conforms to spec-format v1 and so NO existing "
        "requirement is deleted or weakened (the se3 spec guardrails forbid "
        "deleting or weakening a requirement). Re-apply the intended spec update "
        "without dropping any '### Requirement:' that existed before this flow.\n"
    )
