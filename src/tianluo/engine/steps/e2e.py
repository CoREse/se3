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
from typing import Any, Dict, Optional

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
        bootstrap_note = _ensure_content(project_root, flow)
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
    step.error_message = t(
        "e2e.step.scenarios_failed",
        count=len(failed_names),
        scenarios=", ".join(failed_names) or "-",
    )
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


def _ensure_content(project_root: Path, flow: FlowInstance) -> str:
    """Make sure ``tianluo/e2e/`` exists before the session tries to read it.

    First generation (the switch is on but the content directory does not exist
    yet) and incremental evolution are the bootstrap module's job. Returns a short
    note for the step summary, or an empty string when nothing was generated.

    The import is guarded: the content directory is authored by the flow and, once
    present, needs no generation at all, so an unavailable bootstrap module must
    not stop a project whose content is already in place — the session then
    reports a locating ``E2EConfigError`` if it really is missing.
    """
    try:
        from ...e2e import bootstrap  # type: ignore[attr-defined]
    except ImportError:
        logger.debug("e2e bootstrap module unavailable; skipping content generation")
        return ""

    ensure = getattr(bootstrap, "ensure_content", None)
    if not callable(ensure):
        logger.debug("e2e bootstrap module exposes no ensure_content; skipping")
        return ""

    result = ensure(project_root, flow)
    if result is None:
        return ""
    if getattr(result, "created", False):
        from ...e2e.content_config import content_relpath

        return t(
            "e2e.step.content_bootstrapped",
            directory=str(content_relpath(project_root)),
        )
    return str(getattr(result, "note", "") or "")


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
