"""tianluo.e2e.session — orchestrates one complete e2e run.

:func:`run_e2e` is the *single* entry point shared by the engine's ``E2E`` step
and the ``luo e2e`` command, so a scenario behaves identically whether a flow ran
it or a developer did by hand. There is deliberately no second execution path.

Order of operations::

    preflight (runtime probe)         -> environment error short-circuits here
    load + validate the two configs
    select scenarios
    create  (network + images)
    start   (containers)
    wait_ready per service
    run each selected scenario
    collect logs for failures
    destroy                           (in a finally; skipped on keep_environment)

The two failure kinds are kept strictly apart, because the flow engine routes
them in opposite directions: a *scenario* failure is a defect in the code under
test and belongs in the ordinary fix loop, while an *environment* failure (no
usable container runtime, a service that never becomes ready) is the host's
problem and must never dispatch an LLM to repair a machine.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from tianluo.i18n import t

from .backend import EnvironmentHandle, EnvironmentSpec, IsolationBackend
from .content_config import E2EContent, content_relpath, load_content_config
from .errors import E2EConfigError, E2EEnvironmentError, E2EScenarioFailure
from .executor import Executor, ScenarioResult
from .readiness import read_log_tail, wait_ready

logger = logging.getLogger(__name__)

__all__ = [
    "E2EVerdict",
    "FIX_REASON",
    "run_e2e",
]

# The `fix_context.reason` the implement step keys its fix guidance off, mirroring
# the test step's own reason values.
FIX_REASON = "e2e_failure"

# Container log tail attached to a failed scenario's fix instructions.
LOG_TAIL_LINES = 40


@dataclass
class E2EVerdict:
    """The classified result of one e2e run.

    Shaped to mirror :class:`tianluo.engine.steps.test.TestVerdict` field for
    field, so the ``E2E`` step handler is a thin mapping with no logic of its
    own and the two check steps cannot drift apart:

    ======================  =====================================================
    ``TestVerdict``         ``E2EVerdict``
    ======================  =====================================================
    ``test_results``        ``summary`` — the structured dict the handler writes
                            verbatim to ``step.outputs["e2e_results"]``
    ``overall_passed``      ``passed``
    ``should_fix``          ``should_fix`` (a property: true only for scenario
                            failures, never for environment failures)
    ``fix_instructions``    ``fix_instructions``
    ``fix_context``         ``fix_context`` (``reason="e2e_failure"``)
    ``inherited_list``      *(no counterpart)* — e2e has no baseline-inheritance
                            mechanism; every failing scenario is fixed in place
    *(no counterpart)*      ``environment_error`` / ``remediation`` — the routing
                            firewall: set means FAILED + guidance, not the fix
                            loop
    ======================  =====================================================
    """

    passed: bool
    environment_error: str = ""
    remediation: str = ""
    scenario_results: List[ScenarioResult] = field(default_factory=list)
    fix_instructions: str = ""
    fix_context: Dict[str, Any] = field(default_factory=dict)
    summary: Dict[str, Any] = field(default_factory=dict)
    # Localized lines the caller must show the user regardless of the outcome —
    # currently the kept-environment cleanup commands. WHY not logging: the `luo`
    # CLI installs no logging handler, so anything below WARNING is dropped
    # outright and a user who asked to keep the environment would be left with
    # live containers and nothing on screen naming them.
    notices: List[str] = field(default_factory=list)

    @property
    def should_fix(self) -> bool:
        """Whether this verdict must enter the fix loop.

        An environment failure is excluded on purpose: no code change makes a
        missing container runtime appear, so routing it into the loop would burn
        the whole ``workflow.max_fix_iterations`` budget for nothing.
        """
        return not self.passed and not self.environment_error

    @property
    def failed_scenarios(self) -> List[ScenarioResult]:
        return [result for result in self.scenario_results if not result.passed]

    def as_failure(self) -> Optional[E2EScenarioFailure]:
        """The exception form, for callers that prefer raising (the CLI)."""
        if not self.should_fix:
            return None
        names = ", ".join(result.name for result in self.failed_scenarios)
        return E2EScenarioFailure(
            t("e2e.session.scenarios_failed", scenarios=names or "-"),
            results=self.scenario_results,
        )


BackendFactory = Callable[..., IsolationBackend]


def run_e2e(
    project_root: Path,
    *,
    scenarios: Optional[Sequence[str]] = None,
    config: Any = None,
    content: Optional[E2EContent] = None,
    backend: Optional[IsolationBackend] = None,
    backend_factory: Optional[BackendFactory] = None,
    probe: Any = None,
    runner: Callable[..., Any] = subprocess.run,
    network_suffix: str = "",
    artifacts_dir: Optional[Path] = None,
    keep_environment: Optional[bool] = None,
    write_missing_baselines: bool = False,
    llm_factory: Optional[Callable[[], Any]] = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> E2EVerdict:
    """Run the project's selected e2e scenarios and classify the outcome.

    Returns an :class:`E2EVerdict` for both success and *environment* failure —
    the latter carries ``environment_error`` plus ``remediation`` instead of
    raising, so the step handler and the CLI share one result shape.
    :class:`~tianluo.e2e.errors.E2EConfigError` does propagate: malformed
    content is the flow's own artefact and its message locates the offending
    file and YAML path.
    """
    root = Path(project_root)
    if config is None:
        from tianluo.config import E2EConfig

        config = E2EConfig.load(root)

    keep = (
        keep_environment
        if keep_environment is not None
        else bool(getattr(config, "keep_environment", False))
    )

    handle: Optional[EnvironmentHandle] = None
    live_backend: Optional[IsolationBackend] = None
    results: List[ScenarioResult] = []
    # Held so the `finally` can attach the kept-environment notice to whatever
    # verdict is on its way out; mutating it there is still visible to the caller.
    verdict: Optional[E2EVerdict] = None

    try:
        # INVARIANT: preflight comes first and nothing is created before it
        # passes. Building images or a network on a host that cannot run
        # containers leaves half-made resources behind for a failure that was
        # knowable in one cheap `info` call.
        resolved_probe = probe
        if backend is None and resolved_probe is None:
            from .runtime_probe import preflight

            resolved_probe = preflight(config, runner=runner)

        bundle = (
            content
            if content is not None
            # An explicitly requested first capture must survive content loading:
            # the baseline it is about to write does not exist yet by definition.
            else load_content_config(
                root, require_baselines=not write_missing_baselines
            )
        )
        if bundle is None:
            raise E2EConfigError(
                t(
                    "e2e.session.not_bootstrapped",
                    directory=str(content_relpath(root)),
                )
            )

        critical = _critical_names(bundle, config)
        selected = _select_scenarios(bundle, config, scenarios, critical)
        if not selected:
            # Reported rather than silently green: "nothing selected" and
            # "everything passed" must not look the same in the step output.
            summary = _build_summary(
                bundle, [], selected_names=[], runtime="", critical=critical,
                environment_duration=0.0,
            )
            logger.warning("e2e ran no scenario: selection matched nothing")
            verdict = E2EVerdict(passed=True, summary=summary)
            return verdict

        live_backend = backend
        if live_backend is None:
            factory = backend_factory or _default_backend_factory
            live_backend = factory(
                resolved_probe,
                runner=runner,
                oci_runtime=getattr(config, "oci_runtime", None),
                build_timeout=float(getattr(config, "build_timeout", 1800) or 1800),
            )

        spec = _environment_spec(bundle, network_suffix)
        # Measured separately from scenario time: image builds and readiness
        # waits dominate a cold run, and a user looking at a slow e2e step needs
        # to see which half of it was slow — the aggregate scenario duration
        # alone cannot distinguish "the suite is heavy" from "the image rebuilt".
        environment_started = clock()
        handle = live_backend.create(spec)
        live_backend.start(handle)
        _await_services(live_backend, handle, spec)
        environment_duration = max(clock() - environment_started, 0.0)

        executor = Executor(
            live_backend,
            bundle,
            config,
            handle=handle,
            clock=clock,
            sleeper=sleeper,
            artifacts_dir=Path(artifacts_dir) if artifacts_dir else None,
            llm_factory=llm_factory,
            write_missing_baselines=write_missing_baselines,
        )

        for scenario in selected:
            result = executor.run_scenario(scenario)
            if not result.passed:
                # Captured while the containers are still alive: the logs are the
                # single most useful thing in the fix instructions, and after
                # destroy they are gone for good.
                result.logs = _collect_logs(
                    live_backend, handle, spec, result.driver
                )
            results.append(result)
            logger.info("e2e %s", result.summary_line())

        runtime_name = getattr(resolved_probe, "name", "") or getattr(
            handle, "runtime", ""
        )
        verdict = _verdict_for(
            bundle, results, [s.name for s in selected], runtime_name, critical,
            environment_duration=environment_duration,
        )
        return verdict

    except E2EEnvironmentError as exc:
        # Uniform funnel for every host-side problem — an unusable runtime, a
        # service that never came up, a missing optional extra. All of them mean
        # "the machine cannot run e2e", so all of them return a verdict the
        # handler maps to FAILED with guidance rather than to the fix loop.
        logger.warning("e2e environment error: %s", exc.message)
        verdict = E2EVerdict(
            passed=False,
            environment_error=exc.message,
            remediation=exc.remediation,
            scenario_results=results,
            summary={
                "environment_error": exc.message,
                "remediation": exc.remediation,
                "scenarios": [result.to_dict() for result in results],
            },
        )
        return verdict

    finally:
        # INVARIANT: teardown runs on every exit path, including an exception
        # raised mid-scenario. A leaked container keeps a port and an image layer
        # busy and makes the *next* run fail for a reason unrelated to the code.
        target = handle
        if target is None and live_backend is not None:
            # `create` raised partway: it never returned the handle, so the
            # network and images it already made are only reachable through the
            # backend's published record of them.
            target = getattr(live_backend, "last_handle", None)
        if target is not None and live_backend is not None:
            if keep:
                notice = t(
                    "e2e.session.kept_environment",
                    network=target.spec.network,
                    containers=", ".join(target.containers) or "-",
                    runtime=target.runtime,
                )
                # WHY warning + notice rather than info: `luo` installs no
                # logging handler, so an info record is discarded and the user
                # would never learn which containers survived. The notice is what
                # the CLI prints; the warning covers non-CLI callers.
                logger.warning("%s", notice)
                if verdict is not None:
                    verdict.notices.append(notice)
                    verdict.summary["kept_environment"] = {
                        "network": target.spec.network,
                        "containers": sorted(target.containers),
                        "runtime": target.runtime,
                        "cleanup": notice,
                    }
            else:
                try:
                    live_backend.destroy(target)
                except Exception as exc:  # pragma: no cover - destroy is lenient
                    logger.warning("e2e teardown could not finish: %s", exc)


def _default_backend_factory(probe: Any, **kwargs: Any) -> IsolationBackend:
    """Build the container backend, imported lazily.

    Deferred so a caller that injects its own backend (tests, a future VM
    backend) never pays for the container implementation, and so importing this
    module stays free of it.
    """
    from .container_backend import ContainerBackend

    return ContainerBackend(probe, **kwargs)


def _critical_names(content: E2EContent, config: Any) -> List[str]:
    """The configured ``critical_scenarios``, checked against the declared set.

    A critical name that no scenario answers to cannot possibly "genuinely run",
    so the promise the key makes is unsatisfiable and the run stops with a
    locating configuration error. Deliberately *not* routed into the fix loop:
    ``critical_scenarios`` lives in ``tianluo.yaml``, which the flow never
    writes, so no code change the implementing agent can make would fix it.
    """
    critical = [str(name) for name in (getattr(config, "critical_scenarios", None) or [])]
    if not critical:
        return []
    declared = {scenario.name for scenario in content.scenarios}
    unknown = [name for name in critical if name not in declared]
    if unknown:
        raise E2EConfigError(
            t(
                "e2e.session.unknown_critical_scenarios",
                scenarios=", ".join(unknown),
                known=", ".join(sorted(declared)) or "-",
            )
        )
    # De-duplicated, declaration order preserved, so the augmentation below is
    # stable no matter how the user ordered the key.
    return [scenario.name for scenario in content.scenarios if scenario.name in critical]


def _select_scenarios(
    content: E2EContent,
    config: Any,
    requested: Optional[Sequence[str]],
    critical: Sequence[str] = (),
) -> List[Any]:
    """Resolve which scenarios this run executes.

    An explicit ``requested`` list (``luo e2e run --scenario``) wins over the
    configured selection. Unknown names are an error rather than an empty run:
    a typo that silently selects nothing would report a passing e2e step for a
    suite that never executed.

    INVARIANT: every scenario named in ``critical_scenarios`` is appended to
    whatever the selection resolved to. That is what makes "must genuinely run
    for the result to count" true by construction rather than by hope — narrowing
    ``scenarios`` to speed up a fix loop, or passing ``--scenario`` while
    debugging, must not be able to quietly exclude a scenario the user declared
    load-bearing. Anything already selected is not run twice.
    """
    declared = {scenario.name: scenario for scenario in content.scenarios}

    def with_critical(chosen: List[Any]) -> List[Any]:
        names = {scenario.name for scenario in chosen}
        added = [declared[name] for name in critical if name not in names]
        if added:
            logger.info(
                "e2e selection extended with critical scenario(s): %s",
                ", ".join(scenario.name for scenario in added),
            )
        return chosen + added

    if requested:
        names = [str(name) for name in requested]
        unknown = [name for name in names if name not in declared]
        if unknown:
            raise E2EConfigError(
                t(
                    "e2e.session.unknown_scenarios",
                    scenarios=", ".join(unknown),
                    known=", ".join(declared) or "-",
                )
            )
        return with_critical([declared[name] for name in names])

    configured = list(getattr(config, "scenarios", None) or [])
    if configured:
        unknown = [name for name in configured if name not in declared]
        if unknown:
            raise E2EConfigError(
                t(
                    "e2e.session.unknown_scenarios",
                    scenarios=", ".join(unknown),
                    known=", ".join(declared) or "-",
                )
            )
        return with_critical([declared[name] for name in configured])

    return list(content.scenarios)


def _environment_spec(content: E2EContent, suffix: str) -> EnvironmentSpec:
    """Build the backend spec, isolating this run's network by suffix.

    The suffix (the flow id, in flow context) keeps two concurrent worktree runs
    of the same project from joining one another's network and resolving a peer
    service name to the wrong container.
    """
    network = content.network
    if suffix:
        cleaned = "".join(
            char if char.isalnum() or char == "-" else "-" for char in str(suffix)
        ).strip("-").lower()
        if cleaned:
            network = "{}-{}".format(network, cleaned)[:63].rstrip("-")
    return content.to_environment_spec(network=network, labels={"tianluo-e2e": "1"})


def _await_services(
    backend: IsolationBackend, handle: EnvironmentHandle, spec: EnvironmentSpec
) -> None:
    """Confirm every declared readiness probe passes before scenarios start.

    WHY here as well as in the backend: readiness is a guarantee of *the
    session*, not of one backend implementation — a future backend that forgets
    it would otherwise let scenarios race a half-started service and produce
    flaky failures blamed on the code. Re-probing an already-ready service costs
    one cheap check, since a passing probe returns on its first attempt.
    """
    for service in spec.services:
        if service.readiness is not None:
            wait_ready(backend, handle, service.name, service.readiness)


def _collect_logs(
    backend: IsolationBackend,
    handle: EnvironmentHandle,
    spec: EnvironmentSpec,
    driver: str,
) -> str:
    """Log tails of every service in the environment, labelled by service.

    WHY not the driver alone: in the primary web topology the driver is the
    Playwright container, so its log carries the *client* side of the failure —
    while the stack trace that actually explains a failed dom/http assertion sits
    in the application service's log. Sending the fix loop only the browser's
    output makes the iteration guess at a server-side cause it was never shown.
    Declaration order puts the application services ahead of the driver, which is
    also the order a reader wants them in.
    """
    sections: List[str] = []
    for service in spec.services:
        tail = read_log_tail(backend, handle, service.name, lines=LOG_TAIL_LINES)
        if not tail.strip():
            continue
        label = service.name + (" (driver)" if service.name == driver else "")
        sections.append("--- {} ---\n{}".format(label, tail.rstrip()))
    return "\n".join(sections)


def _verdict_for(
    content: E2EContent,
    results: Sequence[ScenarioResult],
    selected_names: Sequence[str],
    runtime: str,
    critical: Sequence[str] = (),
    *,
    environment_duration: float = 0.0,
) -> E2EVerdict:
    failed = [result for result in results if not result.passed]
    # INVARIANT: a critical scenario that produced no result must not count as a
    # pass — the same "a skip is not a pass" guard `test.critical_tests` applies.
    # Selection force-includes the critical set, so reaching here means the run
    # was cut short (a scenario raised, a selection path was bypassed); either
    # way nothing verified what the user declared load-bearing.
    unverified = _unverified_critical(results, critical)
    summary = _build_summary(
        content, results, selected_names, runtime, critical,
        environment_duration=environment_duration,
    )
    if not failed and not unverified:
        return E2EVerdict(passed=True, scenario_results=list(results), summary=summary)

    return E2EVerdict(
        passed=False,
        scenario_results=list(results),
        fix_instructions=_fix_instructions(failed, unverified),
        fix_context=_fix_context(failed, unverified),
        summary=summary,
    )


def _unverified_critical(
    results: Sequence[ScenarioResult], critical: Sequence[str]
) -> List[str]:
    """Critical scenario names with no passing result of their own."""
    passed_names = {result.name for result in results if result.passed}
    return [name for name in critical if name not in passed_names]


def _build_summary(
    content: E2EContent,
    results: Sequence[ScenarioResult],
    selected_names: Sequence[str],
    runtime: str,
    critical: Sequence[str] = (),
    *,
    environment_duration: float = 0.0,
) -> Dict[str, Any]:
    """The structured record written to ``step.outputs["e2e_results"]``."""
    failed = [result for result in results if not result.passed]
    return {
        "runtime": runtime,
        "declared_scenarios": [scenario.name for scenario in content.scenarios],
        "selected_scenarios": list(selected_names),
        "critical_scenarios": list(critical),
        # Named separately from `scenarios_failed`: a critical scenario that
        # never ran is a different diagnosis from one that ran and failed.
        "critical_unverified": [
            name
            for name in _unverified_critical(results, critical)
            if name not in {result.name for result in failed}
        ],
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "scenarios_passed": [r.name for r in results if r.passed],
        "scenarios_failed": [r.name for r in failed],
        "duration": round(sum(result.duration for result in results), 3),
        # The other half of the wall clock: building images, starting containers
        # and waiting for every readiness probe.
        "environment_duration": round(float(environment_duration), 3),
        "artifacts": sorted(
            {artifact for result in results for artifact in result.artifacts}
        ),
        "scenarios": [result.to_dict() for result in results],
    }


def _fix_instructions(
    failed: Sequence[ScenarioResult], unverified_critical: Sequence[str] = ()
) -> str:
    """LLM-facing description of what broke.

    Deliberately not localized: like every other prompt payload in the engine,
    this is written *for the implementing agent*, and it quotes the expected and
    actual values verbatim so nothing has to be re-derived from prose.
    """
    lines: List[str] = []
    missing = [
        name
        for name in unverified_critical
        if name not in {result.name for result in failed}
    ]
    if missing:
        lines += [
            "These scenarios are declared critical in e2e.critical_scenarios but "
            "produced no passing result, so this run does not count as verified: "
            + ", ".join(missing),
            "",
        ]
    if failed:
        lines += [
            "{} e2e scenario(s) failed. Fix the code under test so every "
            "assertion holds.".format(len(failed)),
            "",
        ]
    for result in failed:
        lines.append("## Scenario: {} ({})".format(result.name, result.source))
        lines.append("driver: {}".format(result.driver))
        if result.timed_out:
            lines.append(
                "The scenario exceeded its time budget after {:.1f}s — treat this "
                "as a hang or a missing readiness condition, not as a slow "
                "machine.".format(result.duration)
            )
        if result.error:
            lines.append("error: {}".format(result.error))
        for note in result.action_failures:
            lines.append("action problem: {}".format(note))
        # The action-sequence trace (a declared command that exited non-zero, a
        # browser shot that was taken) is frequently the *cause* of the assertion
        # that failed further down. Without it the fix iteration debugs the
        # symptom with the root cause missing from its prompt.
        for note in result.notes:
            lines.append("action note: {}".format(note))

        failures = result.failed_assertions
        if failures:
            lines.append("")
            lines.append("Failed assertions:")
            for assertion in failures:
                lines.append("- {} (tier {})".format(assertion.kind, assertion.tier))
                lines.append("  expected: {}".format(assertion.expected or "-"))
                lines.append("  actual:   {}".format(assertion.actual or "-"))
                if assertion.message:
                    lines.append("  note:     {}".format(assertion.message))
                if assertion.evidence:
                    lines.append("  evidence: {}".format(assertion.evidence))
        if result.logs:
            lines.append("")
            lines.append("Container log tails (driver: {}):".format(result.driver))
            lines.append(result.logs)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _fix_context(
    failed: Sequence[ScenarioResult], unverified_critical: Sequence[str] = ()
) -> Dict[str, Any]:
    """Structured fix context consumed by the implement step."""
    issues: List[Dict[str, Any]] = []
    for result in failed:
        for failure in result.action_failures:
            # An action that could not be performed is its own issue: the
            # assertion list may look untouched (or even green) while the
            # sequence never reached the state it was written to verify.
            issues.append(
                {
                    "scenario": result.name,
                    "source": result.source,
                    "driver": result.driver,
                    "kind": "action",
                    "tier": 0,
                    "expected": "the declared action sequence runs to completion",
                    "actual": failure,
                    "message": failure,
                    "evidence": "; ".join(result.notes),
                }
            )
        for assertion in result.failed_assertions:
            issues.append(
                {
                    "scenario": result.name,
                    "source": result.source,
                    "driver": result.driver,
                    "kind": assertion.kind,
                    "tier": assertion.tier,
                    "expected": assertion.expected,
                    "actual": assertion.actual,
                    "message": assertion.message,
                    "evidence": assertion.evidence,
                }
            )
        if not result.failed_assertions and not result.action_failures:
            # A scenario can fail without a failing assertion — a timeout, or a
            # driver error before any assertion ran. Recording it keeps the
            # issue list a faithful account of every failure.
            issues.append(
                {
                    "scenario": result.name,
                    "source": result.source,
                    "driver": result.driver,
                    "kind": "scenario",
                    "tier": 0,
                    "expected": "scenario completes and asserts",
                    "actual": result.error
                    or "; ".join(result.action_failures)
                    or "no assertion was evaluated",
                    "message": result.error,
                    "evidence": "",
                }
            )

    failed_names = [result.name for result in failed]
    missing = [name for name in unverified_critical if name not in failed_names]
    for name in missing:
        issues.append(
            {
                "scenario": name,
                "source": "tianluo.yaml: e2e.critical_scenarios",
                "driver": "-",
                "kind": "critical_scenario_not_verified",
                "tier": 0,
                "expected": "critical scenario '{}' runs and passes".format(name),
                "actual": "it produced no result in this run",
                "message": "A scenario declared critical must genuinely run; a "
                "missing result does not count as a pass.",
                "evidence": "",
            }
        )
    return {
        "reason": FIX_REASON,
        "scenarios_failed": failed_names,
        "critical_unverified": missing,
        "issues": issues,
    }
