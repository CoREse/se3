"""E2E step handler.

Runs the project's declarative e2e scenarios in a real isolated environment
(container topology built by docker/podman) and maps the outcome onto the two
routes the flow engine already knows:

* every selected scenario passed → ``COMPLETED``;
* a scenario's assertions did not hold → ``REVISION_NEEDED``, which the state
  machine routes into the ordinary fix loop bounded by
  ``workflow.max_fix_iterations`` — a failing scenario is a code defect exactly
  like a failing unit test, with no discard / waiver / severity channel;
* the *host* cannot run e2e (no usable container runtime, the current user lacks
  permission, the ``tianluo[e2e]`` extra is missing) → ``FAILED`` carrying
  remediation guidance, and deliberately NOT the fix loop: no code change makes
  a missing container runtime appear, so routing it into the loop would dispatch
  the implementing agent at the operator's machine and burn the whole fix budget.

Before running anything the step maintains ``tianluo/e2e/`` — generating it on
first use and evolving it incrementally on every later flow (see
:func:`_maintain_content`). The content is the flow's artefact exactly like test
code is, so leaving it at whatever the first flow authored would let the suite
report green over behaviour nothing covers.

The step is only ever part of a sequence when ``e2e.enabled`` is true (see
``config.insert_e2e_step``); the disabled branch here is defensive, for a
persisted flow whose sequence was derived while the switch was on.

WHY every ``tianluo.e2e`` import sits inside a function body: the charter's
core/extra dependency isolation. ``tianluo.engine.steps`` is imported by
``luo run`` on every invocation, so a module-level import of the e2e subsystem
would pull the container backend — and, transitively, anything behind the
``tianluo[e2e]`` extra — into every core-only install. Deferring them keeps a
core install free of e2e and turns a missing extra into an actionable install
hint (``E2EDependencyMissingError``) instead of an ``ImportError`` on an
unrelated command.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...i18n import t
from ..models import FlowInstance, Step, StepStatus
from ._project_root import resolve_flow_project_root

logger = logging.getLogger(__name__)


def e2e_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the e2e step.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        ``StepStatus.COMPLETED`` when every selected scenario passed (or e2e is
        disabled), ``StepStatus.REVISION_NEEDED`` on a scenario failure (drives
        the fix loop), ``StepStatus.FAILED`` on an environment or configuration
        problem.
    """
    from ...config import E2EConfig

    project_root = resolve_flow_project_root(flow)
    config = E2EConfig.load(project_root)

    if not config.enabled:
        # Defensive: insert_e2e_step keeps E2E out of the sequence entirely while
        # the switch is off, so this is only reachable for a flow persisted while
        # it was on. Skipping (rather than failing) lets such a flow resume.
        logger.info("e2e step reached with e2e.enabled false — skipping")
        step.outputs["e2e_results"] = {"skipped": True, "reason": "disabled"}
        step.outputs["scenarios_passed"] = []
        step.outputs["scenarios_failed"] = []
        return StepStatus.COMPLETED

    from ...e2e.errors import E2EConfigError, E2EEnvironmentError
    from ...e2e.session import run_e2e

    try:
        bootstrap_note = _maintain_content(project_root, step, flow)
        verdict = run_e2e(
            project_root,
            config=config,
            # Isolates this run's container network: two concurrent worktree
            # flows of one project must not join each other's network and
            # resolve a peer service name to the wrong container.
            network_suffix=flow.flow_id or "",
            artifacts_dir=_artifacts_dir(project_root, flow),
        )
    except E2EEnvironmentError as exc:
        # E2EDependencyMissingError subclasses this, so the missing-extra case
        # (with its `pip install 'tianluo[e2e]'` remediation) lands here too —
        # same blame, same route, no second branch.
        return _fail_environment(step, exc.message, exc.remediation)
    except E2EConfigError as exc:
        # The content config is the flow's own artefact and its message locates
        # the offending file plus YAML path. It is NOT routed into the fix loop:
        # the implement step fixes the code under test, and a malformed
        # environment declaration goes through the ordinary human escalation
        # channel a FAILED step opens.
        return _fail_config(step, str(exc))

    if verdict.environment_error:
        # run_e2e funnels host-side problems into a verdict rather than raising,
        # so both shapes have to be handled — this is the same failure, arriving
        # as data.
        return _fail_environment(
            step, verdict.environment_error, verdict.remediation, verdict.summary
        )

    summary = dict(verdict.summary or {})
    if bootstrap_note:
        summary["bootstrap"] = bootstrap_note

    step.outputs["e2e_results"] = summary
    step.outputs["e2e_passed"] = verdict.passed
    step.outputs["scenarios_passed"] = list(summary.get("scenarios_passed", []))
    step.outputs["scenarios_failed"] = list(summary.get("scenarios_failed", []))

    if not verdict.should_fix:
        return StepStatus.COMPLETED

    failed_names = [result.name for result in verdict.failed_scenarios]
    # A critical scenario that produced no result forces `passed=False` with an
    # empty failed list, so composing the headline from the failed list alone
    # would announce "0 e2e scenario(s) failed" as the reason for a revision —
    # a self-contradiction wherever error_message is the only thing on screen
    # (WebUI, history). Each cause states itself, and both can hold at once.
    unverified = [
        str(name) for name in (summary.get("critical_unverified") or [])
    ]
    reasons = []
    if failed_names:
        reasons.append(
            t(
                "e2e.step.scenarios_failed",
                count=len(failed_names),
                scenarios=", ".join(failed_names),
            )
        )
    if unverified:
        reasons.append(
            t(
                "e2e.step.critical_unverified",
                count=len(unverified),
                scenarios=", ".join(unverified),
            )
        )
    step.error_message = " ".join(reasons) or t("e2e.step.not_passed")
    step.outputs["fix_needed"] = True
    step.outputs["fix_instructions"] = verdict.fix_instructions
    step.outputs["fix_context"] = _describe_issues(verdict.fix_context)
    return StepStatus.REVISION_NEEDED


def _describe_issues(fix_context: Dict[str, Any]) -> Dict[str, Any]:
    """Give each e2e issue the ``description`` the engine's renderers read.

    WHY: ``fix_history`` entries are re-rendered into the implement prompt of
    every LATER fix iteration through the engine's shared
    ``extract_issue_display_fields``, which knows the self_check and verify_spec
    issue schemas — an e2e issue keyed by scenario/kind/expected/actual would
    render as an empty bullet, so iteration 2 onwards would see that something
    failed but not what. Composing the sentence here (rather than teaching the
    generic extractor a third schema) keeps the e2e shape out of the engine's
    core while the prompt stays informative.

    Not localized on purpose: like ``fix_instructions``, this text is written for
    the implementing agent, not for a human reading the console.
    """
    issues = fix_context.get("issues")
    if not isinstance(issues, list):
        return fix_context

    for issue in issues:
        if not isinstance(issue, dict) or issue.get("description"):
            continue
        detail = "{} (tier {}) expected {!r}, actual {!r}".format(
            issue.get("kind", "assertion"),
            issue.get("tier", "?"),
            issue.get("expected", ""),
            issue.get("actual", ""),
        )
        note = issue.get("message") or ""
        issue["description"] = "scenario '{}': {}{}".format(
            issue.get("scenario", "?"), detail, " — " + note if note else ""
        )
        if not issue.get("location"):
            issue["location"] = str(issue.get("source") or issue.get("driver") or "")
    return fix_context


def _fail_environment(
    step: Step,
    message: str,
    remediation: str,
    summary: Optional[Dict[str, Any]] = None,
) -> StepStatus:
    """Record a host-side e2e failure as FAILED + guidance, never as a fix.

    INVARIANT: ``fix_needed`` is deliberately NOT written here. It is the single
    flag ``_transition_to_fix`` consults, so leaving it unset is what keeps an
    unusable container runtime out of the fix loop and out of the fix-iteration
    counter.
    """
    logger.error("e2e environment failure: %s", message)
    step.outputs["environment_error"] = message
    step.outputs["e2e_remediation"] = remediation
    step.outputs["e2e_results"] = dict(summary or {}) or {
        "environment_error": message,
        "remediation": remediation,
    }
    step.outputs["scenarios_passed"] = list(
        (summary or {}).get("scenarios_passed", []) or []
    )
    step.outputs["scenarios_failed"] = list(
        (summary or {}).get("scenarios_failed", []) or []
    )
    step.error_message = "\n".join(part for part in (message, remediation) if part)
    return StepStatus.FAILED


def _fail_config(step: Step, message: str) -> StepStatus:
    """Record a malformed e2e configuration as FAILED (no fix loop)."""
    logger.error("e2e configuration error: %s", message)
    step.outputs["environment_error"] = message
    step.outputs["e2e_results"] = {"config_error": message}
    step.outputs["scenarios_passed"] = []
    step.outputs["scenarios_failed"] = []
    step.error_message = message
    return StepStatus.FAILED


# Marker in the flow's shared context recording that this flow has already had
# its one content-evolution pass. Lives in the persisted context (not in
# ``step.outputs``) because the E2E step is re-executed from scratch on every fix
# iteration, and the whole point of the guard is to survive exactly that.
_EVOLVED_CONTEXT_KEY = "e2e_content_evolved"

# Enough changed paths for the model to see the shape of the task, far short of
# pasting a large refactor's whole file list into the prompt.
_MAX_HINT_FILES = 40


def _maintain_content(project_root: Path, step: Step, flow: FlowInstance) -> str:
    """Bring ``tianluo/e2e/`` in step with the code this flow just changed.

    The flow owns this directory the same way it owns test code, which means two
    halves of one job — and only doing the first half is how an e2e suite quietly
    rots:

    * **first use** (the switch is on but the directory does not exist yet) →
      ``ensure_content`` authors it against the current task;
    * **every later flow** → ``evolve_content`` extends and revises it, so a task
      that adds a user-visible behaviour also grows the scenario exercising it.
      Without this half the suite freezes at whatever the very first flow wrote,
      and each later run reports a green board for code that nothing covers —
      the most expensive kind of green, because it looks verified.

    Evolution happens BEFORE the scenarios run, so anything it adds is exercised
    in this same flow and a failure it exposes routes into the ordinary fix loop.

    INVARIANT: at most one evolution pass per flow, guarded by
    ``_EVOLVED_CONTEXT_KEY``. Re-evolving on every fix iteration would (a) spend
    an LLM call per iteration on a task whose code has barely moved, and (b) show
    the model the very assertion currently failing while it is being asked to
    revise scenarios — an open invitation to make the suite pass by weakening it,
    which is precisely the bypass the charter forbids.

    Returns a short note for the step summary, or ``""`` when nothing changed.
    The bootstrap import is guarded: content that is already in place needs no
    generation at all, so an unavailable bootstrap module must not stop a project
    whose directory is complete — the session then reports a locating
    ``E2EConfigError`` if it really is missing.
    """
    try:
        from ...e2e import bootstrap  # type: ignore[attr-defined]
    except ImportError:
        logger.debug("e2e bootstrap module unavailable; skipping content generation")
        return ""

    ensure = getattr(bootstrap, "ensure_content", None)
    if callable(ensure):
        result = ensure(project_root, flow)
        if result is not None and getattr(result, "created", False):
            from ...e2e.content_config import content_relpath

            # Freshly authored against this very task, so there is nothing for an
            # evolution pass to add — and the guard is set so a later fix
            # iteration does not mistake "generated this flow" for "never
            # maintained".
            _mark_evolved(flow)
            return t(
                "e2e.step.content_bootstrapped",
                directory=str(content_relpath(project_root)),
            )
    else:
        logger.debug("e2e bootstrap module exposes no ensure_content; skipping")

    return _evolve_content(project_root, step, flow, bootstrap)


def _evolve_content(
    project_root: Path, step: Step, flow: FlowInstance, bootstrap: Any
) -> str:
    """Run this flow's single incremental-evolution pass over existing content."""
    evolve = getattr(bootstrap, "evolve_content", None)
    if not callable(evolve):
        logger.debug("e2e bootstrap module exposes no evolve_content; skipping")
        return ""
    if _already_evolved(flow):
        return ""

    hints = _evolution_hints(step, flow)
    if not hints:
        # Nothing to say about what changed means nothing to aim an evolution at,
        # and an unaimed pass is pure churn against a suite that already runs.
        logger.debug("no e2e evolution hints available; leaving content as is")
        return ""

    # Marked before the call, not after: the guard means "this flow has had its
    # attempt", so a proposal that fails must not be retried by the next fix
    # iteration with a nearly identical prompt.
    _mark_evolved(flow)
    result = evolve(project_root, flow, hints)
    if result is None:
        return ""

    errors = tuple(getattr(result, "errors", ()) or ())
    if errors:
        # Degrades on purpose: the content already on disk is valid and runnable,
        # so a rejected proposal is reported, never fatal.
        logger.warning("e2e content evolution produced nothing usable: %s", errors[0])
    elif getattr(result, "changed", False):
        logger.info(
            "e2e content evolved: %s", ", ".join(getattr(result, "written", ()) or ())
        )
    if errors or getattr(result, "changed", False):
        return str(getattr(result, "note", "") or "")
    return ""


def _already_evolved(flow: FlowInstance) -> bool:
    context = getattr(getattr(flow, "state", None), "context", None)
    return bool(isinstance(context, dict) and context.get(_EVOLVED_CONTEXT_KEY))


def _mark_evolved(flow: FlowInstance) -> None:
    context = getattr(getattr(flow, "state", None), "context", None)
    if isinstance(context, dict):
        context[_EVOLVED_CONTEXT_KEY] = True


def _evolution_hints(step: Step, flow: FlowInstance) -> List[str]:
    """Describe what this task changed, for the evolution prompt to aim at.

    Deliberately assembled from what the flow already carries (the task
    description and the implement step's ``changes_made``) rather than by
    diffing git: the step's declared inputs are the engine's own account of the
    change, and they stay meaningful in worktree mode where the working tree has
    already moved on.
    """
    inputs = step.inputs if isinstance(step.inputs, dict) else {}
    hints: List[str] = []

    task = str(getattr(flow, "task_description", "") or "").strip()
    if task:
        hints.append("Task implemented in this flow: " + task)

    changes = inputs.get("changes_made")
    if isinstance(changes, dict):
        paths = _changed_paths(changes.get("files_changed"))
        if paths:
            hints.append("Files changed: " + ", ".join(paths[:_MAX_HINT_FILES]))
        groups = _group_labels(changes.get("implemented_groups"))
        if groups:
            hints.append("Implemented groups: " + "; ".join(groups))

    # Sibling input, not part of ``changes_made``: the state machine forwards the
    # implement step's prose summary under its own key.
    summary = str(inputs.get("implement_summary") or "").strip()
    if not summary and isinstance(changes, dict):
        summary = str(changes.get("summary") or "").strip()
    if summary:
        hints.append("Implementation summary: " + summary)

    return hints


def _changed_paths(files_changed: Any) -> List[str]:
    """Normalize ``changes_made.files_changed``, which has two live shapes.

    The implement step emits plain path strings; other producers emit
    ``{"path": ..., "action": ...}`` mappings (see ``version_analyze``).
    """
    if not isinstance(files_changed, (list, tuple)):
        return []
    paths: List[str] = []
    for entry in files_changed:
        if isinstance(entry, str):
            candidate = entry.strip()
        elif isinstance(entry, dict):
            candidate = str(entry.get("path") or "").strip()
        else:
            candidate = ""
        if candidate and candidate not in paths:
            paths.append(candidate)
    return paths


def _group_labels(implemented_groups: Any) -> List[str]:
    """Normalize ``changes_made.implemented_groups``, which also has two shapes.

    Group-by-group execution records bare group ids; the whole-plan path records
    ``{"name": ..., "description": ...}`` mappings. The description is what tells
    the model *what behaviour* appeared, so it is kept when present.
    """
    if not isinstance(implemented_groups, (list, tuple)):
        return []
    labels: List[str] = []
    for entry in implemented_groups:
        if isinstance(entry, str):
            label = entry.strip()
        elif isinstance(entry, dict):
            name = str(entry.get("name") or entry.get("id") or "").strip()
            description = str(entry.get("description") or "").strip()
            label = ": ".join(part for part in (name, description) if part)
        else:
            label = ""
        if label and label not in labels:
            labels.append(label)
    return labels[:_MAX_HINT_FILES]


def _artifacts_dir(project_root: Path, flow: FlowInstance) -> Path:
    """Per-flow directory for screenshots and other captured artefacts.

    Under the runtime log directory rather than inside ``tianluo/e2e/``: that
    directory holds *committed* content configuration and baseline images, and
    dropping per-run captures beside a baseline is how a stale screenshot ends up
    committed as the new baseline.
    """
    from ...runtime_paths import runtime_dir

    return runtime_dir(project_root) / "logs" / "e2e" / (flow.flow_id or "flow")


__all__ = ["e2e_handler"]
