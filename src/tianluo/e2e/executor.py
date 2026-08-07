"""tianluo.e2e.executor — runs one scenario inside its driver container.

The executor is the piece every project shares: scenarios are declarative data
(``tianluo/e2e/scenarios/*.yaml``), and this module is the single engine that
interprets them. A managed project therefore never holds e2e framework code —
it holds declarations and baseline images, and inherits fixes to the engine by
upgrading tianluo.

Responsibilities, in order:

1. resolve the scenario's **driver** — a Playwright service for browser
   scenarios, the application container itself for pure-CLI ones;
2. run the **action sequence** through programmatic entry points (CLI / HTTP /
   DOM events), reserving coordinate-driven input for GUIs that offer none;
3. evaluate every **assertion** by way of :mod:`tianluo.e2e.assertions`,
   collecting all of them rather than stopping at the first failure;
4. enforce the scenario's **time budget** and report exhaustion as a result, not
   as an exception;
5. return a structured :class:`ScenarioResult` the session turns into fix
   instructions.

stdlib only — the tiers that need more import it lazily inside
:mod:`tianluo.e2e.assertions`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from tianluo.i18n import t

from .assertions import (
    AssertionContext,
    AssertionResult,
    BrowserBridge,
    evaluate,
    fetch_http,
)
from .backend import EnvironmentHandle, IsolationBackend
from .content_config import ActionDecl, E2EContent, ScenarioDecl
from .errors import E2EConfigError

logger = logging.getLogger(__name__)

__all__ = [
    "BROWSER_OPS",
    "DriverTarget",
    "Executor",
    "ScenarioResult",
]

# Browser operations the shared Playwright program understands. Every one of
# them is selector- or URL-addressed: that is what keeps web driving on tier 1.
BROWSER_OPS = (
    "goto",
    "click",
    "fill",
    "press",
    "select",
    "wait_for",
    "wait",
    "screenshot",
)

# Base image family that means "this service is a browser driver". The content
# layer already constrains base_kind to the three sanctioned template families.
PLAYWRIGHT_BASE_KIND = "playwright"

_DEFAULT_SCENARIO_TIMEOUT = 300.0
_DEFAULT_ACTION_TIMEOUT = 120.0
_WAIT_POLL_INTERVAL = 1.0


@dataclass(frozen=True)
class DriverTarget:
    """The container a scenario's actions and queries run in.

    ``is_browser`` decides *how* the driver is spoken to, not merely which
    container it is: a Playwright service is driven through one batched browser
    program, while an application container is driven with plain ``exec``.
    """

    service: str
    is_browser: bool
    base_kind: str = "base"


@dataclass
class ScenarioResult:
    """Structured outcome of one scenario.

    ``assertions`` holds every assertion that was evaluated — including the ones
    that passed — because a fix loop needs to know what already works as much as
    what broke. ``evidence`` collects the reviewable justifications (tier-2 diff
    ratios, tier-3 descriptions) and ``artifacts`` the host paths of screenshots
    and other captures.
    """

    name: str
    passed: bool
    assertions: List[AssertionResult] = field(default_factory=list)
    duration: float = 0.0
    evidence: List[str] = field(default_factory=list)
    artifacts: List[str] = field(default_factory=list)
    driver: str = ""
    source: str = ""
    timed_out: bool = False
    error: str = ""
    action_failures: List[str] = field(default_factory=list)
    logs: str = ""

    @property
    def failed_assertions(self) -> List[AssertionResult]:
        return [result for result in self.assertions if not result.passed]

    def summary_line(self) -> str:
        """One-line rendering used by reports and the CLI."""
        state = "PASS" if self.passed else "FAIL"
        return "[{}] {} ({}/{} assertions, {:.1f}s)".format(
            state,
            self.name,
            len(self.assertions) - len(self.failed_assertions),
            len(self.assertions),
            self.duration,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serializable form written into the step's outputs and the WebUI."""
        return {
            "name": self.name,
            "passed": self.passed,
            "driver": self.driver,
            "source": self.source,
            "duration": round(self.duration, 3),
            "timed_out": self.timed_out,
            "error": self.error,
            "action_failures": list(self.action_failures),
            "evidence": list(self.evidence),
            "artifacts": list(self.artifacts),
            "assertions": [
                {
                    "kind": result.kind,
                    "tier": result.tier,
                    "passed": result.passed,
                    "expected": result.expected,
                    "actual": result.actual,
                    "message": result.message,
                    "evidence": result.evidence,
                }
                for result in self.assertions
            ],
        }


class Executor:
    """Runs scenarios of one loaded content bundle against one live environment.

    ``config`` is the :class:`tianluo.config.E2EConfig` (duck-typed: anything
    exposing ``scenario_timeout``), which supplies the default per-scenario
    budget when a scenario declares none.
    """

    def __init__(
        self,
        backend: IsolationBackend,
        content: E2EContent,
        config: Any = None,
        *,
        handle: Optional[EnvironmentHandle] = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        artifacts_dir: Optional[Path] = None,
        llm_factory: Optional[Callable[[], Any]] = None,
        write_missing_baselines: bool = False,
        fail_fast: bool = False,
    ) -> None:
        self.backend = backend
        self.content = content
        self.config = config
        self.handle = handle
        self.clock = clock
        self.sleeper = sleeper
        self.artifacts_dir = Path(artifacts_dir) if artifacts_dir else None
        self.llm_factory = llm_factory
        self.write_missing_baselines = write_missing_baselines
        self.fail_fast = fail_fast

    # ------------------------------------------------------------------
    # driver resolution
    # ------------------------------------------------------------------

    def resolve_driver(self, scenario: ScenarioDecl) -> DriverTarget:
        """Resolve the scenario's declared driver into a concrete target.

        Raises :class:`~tianluo.e2e.errors.E2EConfigError` for a driver that is
        not a declared service: running the scenario somewhere else "so it at
        least runs" would produce a green result for an environment nobody
        described.
        """
        service = self.content.service(scenario.driver)
        if service is None:
            raise E2EConfigError(
                t(
                    "e2e.exec.unknown_driver",
                    driver=scenario.driver,
                    scenario=scenario.name,
                    known=", ".join(s.name for s in self.content.services) or "-",
                )
            )
        return DriverTarget(
            service=service.name,
            is_browser=service.base_kind == PLAYWRIGHT_BASE_KIND,
            base_kind=service.base_kind,
        )

    # ------------------------------------------------------------------
    # scenario execution
    # ------------------------------------------------------------------

    def scenario_budget(self, scenario: ScenarioDecl) -> float:
        if scenario.timeout:
            return float(scenario.timeout)
        configured = getattr(self.config, "scenario_timeout", None)
        return float(configured or _DEFAULT_SCENARIO_TIMEOUT)

    def run_scenario(
        self,
        scenario: ScenarioDecl,
        *,
        handle: Optional[EnvironmentHandle] = None,
    ) -> ScenarioResult:
        """Drive one scenario and return its structured result."""
        live = handle or self.handle
        if live is None:
            raise E2EConfigError(t("e2e.exec.no_environment", scenario=scenario.name))

        driver = self.resolve_driver(scenario)
        started = self.clock()
        budget = self.scenario_budget(scenario)
        ctx = AssertionContext(
            backend=self.backend,
            handle=live,
            driver=driver.service,
            scenario=scenario.name,
            project_root=self.content.project_root,
            baselines_dir=self.content.baselines,
            artifacts_dir=self.artifacts_dir,
            deadline=started + budget,
            clock=self.clock,
            write_missing_baselines=self.write_missing_baselines,
            llm_factory=self.llm_factory,
        )
        if driver.is_browser:
            ctx.browser = BrowserBridge(
                self.backend, live, driver.service, timeout=budget
            )

        result = ScenarioResult(
            name=scenario.name,
            passed=False,
            driver=driver.service,
            source=scenario.source,
        )

        timed_out = self._run_actions(scenario, ctx, result)
        if not timed_out:
            timed_out = self._run_assertions(scenario, ctx, result)

        result.duration = max(self.clock() - started, 0.0)
        result.timed_out = timed_out
        result.artifacts = [str(path) for path in ctx.artifacts]
        result.evidence = [
            item.evidence for item in result.assertions if item.evidence
        ] + list(ctx.notes)
        result.passed = (
            not timed_out
            and bool(result.assertions)
            and all(item.passed for item in result.assertions)
            and not result.error
        )
        if timed_out and not result.error:
            result.error = t(
                "e2e.exec.scenario_timeout",
                scenario=scenario.name,
                seconds=int(budget),
            )
        return result

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------

    def _run_actions(
        self, scenario: ScenarioDecl, ctx: AssertionContext, result: ScenarioResult
    ) -> bool:
        """Execute the action sequence; returns whether the budget ran out.

        Actions that *fail* (a non-zero exit, an unreachable URL) are recorded
        and execution continues: the assertions are what decide the verdict, and
        a command exiting non-zero is frequently the very thing under test.
        """
        for index, action in enumerate(scenario.actions):
            if self._exhausted(ctx):
                return True
            # Config and environment errors deliberately propagate: the session
            # routes an environment problem away from the fix loop, and a broken
            # declaration must not be reported as a defect of the code under test.
            self._run_action(action, ctx, scenario)
            logger.debug(
                "scenario %s action %d (%s) done", scenario.name, index, action.kind
            )

        # A browser scenario's program is assembled from the whole action
        # sequence and executed once, so the queries its DOM assertions need must
        # be registered before it runs — that happens in _run_assertions.
        return self._exhausted(ctx)

    def _run_action(
        self, action: ActionDecl, ctx: AssertionContext, scenario: ScenarioDecl
    ) -> None:
        kind = (action.kind or "").strip()

        if kind == "exec":
            self._action_exec(action, ctx)
            return
        if kind == "http":
            self._action_http(action, ctx)
            return
        if kind == "browser":
            self._action_browser(action, ctx, scenario)
            return
        if kind == "wait":
            self._action_wait(action, ctx)
            return
        if kind == "screenshot":
            self._action_screenshot(action, ctx)
            return
        if kind == "visual_click":
            self._action_visual_click(action, ctx, scenario)
            return

        raise E2EConfigError(
            t(
                "e2e.exec.unknown_action",
                action=action.kind,
                scenario=scenario.name,
                known=", ".join(
                    ("exec", "http", "browser", "wait", "screenshot", "visual_click")
                ),
            )
        )

    def _action_exec(self, action: ActionDecl, ctx: AssertionContext) -> None:
        argv = _argv_of(action.get("command"))
        if not argv:
            raise E2EConfigError(t("e2e.exec.empty_command", scenario=ctx.scenario))
        service = str(action.get("service") or ctx.driver)
        timeout = ctx.timeout_for(float(action.get("timeout") or _DEFAULT_ACTION_TIMEOUT))
        environment = action.get("environment")
        outcome = self.backend.exec(
            ctx.handle,
            service,
            argv,
            timeout=timeout,
            workdir=action.get("workdir"),
            environment=dict(environment) if isinstance(environment, Mapping) else None,
        )
        ctx.record_exec(service, argv, outcome)
        if not getattr(outcome, "ok", False):
            ctx.notes.append(
                "action exec {} in {} exited {}".format(
                    " ".join(argv), service, outcome.exit_code
                )
            )

    def _action_http(self, action: ActionDecl, ctx: AssertionContext) -> None:
        url = str(action.get("url") or "")
        from_service = action.get("from")
        record = fetch_http(
            url,
            ctx=ctx,
            from_service=str(from_service) if from_service else None,
            timeout=action.get("timeout"),
        )
        ctx.last_http = record
        if record.error:
            ctx.notes.append("action http {} failed: {}".format(url, record.error))

    def _action_browser(
        self, action: ActionDecl, ctx: AssertionContext, scenario: ScenarioDecl
    ) -> None:
        if ctx.browser is None:
            raise E2EConfigError(
                t(
                    "e2e.exec.browser_needs_playwright",
                    scenario=scenario.name,
                    driver=ctx.driver,
                )
            )
        op = str(action.get("op") or "")
        if op not in BROWSER_OPS:
            raise E2EConfigError(
                t(
                    "e2e.exec.unknown_browser_op",
                    op=op,
                    scenario=scenario.name,
                    known=", ".join(BROWSER_OPS),
                )
            )
        params = dict(action.params)
        params.pop("op", None)
        if op == "screenshot":
            # Screenshots the browser takes land inside its own container, so the
            # in-container path is remembered under the shot's name: a later
            # tier-2/3 assertion then copies that exact file out instead of
            # capturing a second, different shot of a page that has moved on.
            name = str(params.pop("name", "") or "shot")
            remote = str(params.get("path") or "/tmp/tianluo-e2e-{}.png".format(
                _slug(name)
            ))
            params["path"] = remote
            ctx.remote_screenshots[name] = remote
            ctx.notes.append("browser screenshot {} -> {}".format(name, remote))
        ctx.browser.add_op(op, params)

    def _action_wait(self, action: ActionDecl, ctx: AssertionContext) -> None:
        seconds = action.get("seconds")
        until = action.get("until")
        if until is not None:
            self._wait_until(until, ctx, action)
            return
        try:
            delay = float(seconds or 0)
        except (TypeError, ValueError):
            raise E2EConfigError(
                t("e2e.exec.bad_wait", value=repr(seconds), scenario=ctx.scenario)
            ) from None
        remaining = ctx.remaining()
        if remaining is not None:
            delay = min(delay, max(remaining, 0.0))
        if delay > 0:
            self.sleeper(delay)

    def _wait_until(
        self, until: Any, ctx: AssertionContext, action: ActionDecl
    ) -> None:
        """Poll a command in the driver until it succeeds or the budget ends.

        WHY a command rather than a new probe language: readiness probes already
        cover "is the service up"; ``wait.until`` covers "has the app reached
        the state my next action needs", which is application-specific and best
        expressed as the project's own check command.
        """
        argv = _argv_of(until)
        if not argv:
            raise E2EConfigError(t("e2e.exec.empty_command", scenario=ctx.scenario))
        service = str(action.get("service") or ctx.driver)
        interval = float(action.get("interval") or _WAIT_POLL_INTERVAL)
        while True:
            outcome = self.backend.exec(
                ctx.handle, service, argv, timeout=ctx.timeout_for(_WAIT_POLL_INTERVAL * 30)
            )
            ctx.record_exec(service, argv, outcome)
            if getattr(outcome, "ok", False):
                return
            if self._exhausted(ctx):
                ctx.notes.append(
                    "wait.until {} never succeeded within the scenario budget".format(
                        " ".join(argv)
                    )
                )
                return
            self.sleeper(interval)

    def _action_screenshot(self, action: ActionDecl, ctx: AssertionContext) -> None:
        name = str(action.get("name") or "screenshot")
        service = str(action.get("service") or ctx.driver)
        destination = None
        if self.artifacts_dir is not None:
            destination = self.artifacts_dir / "{}-{}.png".format(
                _slug(ctx.scenario), _slug(name)
            )
        snapshot = self.backend.snapshot(
            ctx.handle,
            service,
            str(action.get("target") or ""),
            kind="screenshot",
            destination=destination,
        )
        ctx.screenshots[name] = Path(snapshot.path)
        ctx.add_artifact(Path(snapshot.path))

    def _action_visual_click(
        self, action: ActionDecl, ctx: AssertionContext, scenario: ScenarioDecl
    ) -> None:
        """Coordinate-driven input — the operation-side floor of the ladder.

        INVARIANT: reserved for GUIs with no programmatic entry point, which is
        why the scenario must declare ``visual_driving``. The schema enforces the
        same rule; the check is repeated here because a declaration built in
        code never passed through it.
        """
        if not scenario.visual_driving:
            raise E2EConfigError(
                t("e2e.exec.visual_driving_undeclared", scenario=scenario.name)
            )
        x, y = action.get("x"), action.get("y")
        button = str(action.get("button") or "1")
        argv = ["xdotool", "mousemove", str(x), str(y), "click", button]
        service = str(action.get("service") or ctx.driver)
        outcome = self.backend.exec(
            ctx.handle, service, argv, timeout=ctx.timeout_for(_DEFAULT_ACTION_TIMEOUT)
        )
        ctx.record_exec(service, argv, outcome)
        if not getattr(outcome, "ok", False):
            ctx.notes.append(
                "visual_click at ({}, {}) failed: {}".format(
                    x, y, (outcome.stderr or outcome.stdout or "").strip()
                )
            )

    # ------------------------------------------------------------------
    # assertions
    # ------------------------------------------------------------------

    def _run_assertions(
        self, scenario: ScenarioDecl, ctx: AssertionContext, result: ScenarioResult
    ) -> bool:
        """Evaluate every assertion; returns whether the budget ran out.

        WHY all of them by default: one e2e round is expensive (image build,
        container start, readiness), so surfacing the complete failure surface in
        a single round is what lets the fix loop converge instead of peeling off
        one assertion per iteration. ``fail_fast`` exists for the explicit,
        declared case where a later assertion cannot be meaningfully evaluated
        after an earlier one fails.
        """
        dom_indices = self._register_dom_queries(scenario, ctx)
        if ctx.browser is not None and ctx.browser.pending:
            ctx.browser.run(timeout=ctx.timeout_for(self.scenario_budget(scenario)))
            for failed in ctx.browser.failed_ops():
                message = "browser {} failed: {}".format(
                    failed.get("op"), failed.get("error")
                )
                result.action_failures.append(message)
            if ctx.browser.error:
                result.action_failures.append(ctx.browser.error)

        fail_fast = self.fail_fast or getattr(scenario, "fail_fast", False)
        for position, assertion in enumerate(scenario.assertions):
            if self._exhausted(ctx):
                return True
            observation = None
            if assertion.kind == "dom" and position in dom_indices:
                observation = ctx.browser.observation(dom_indices[position])
            outcome = evaluate(assertion, ctx, observation=observation)
            result.assertions.append(outcome)
            for artifact in outcome.artifacts:
                ctx.add_artifact(Path(artifact))
            if not outcome.passed and fail_fast:
                ctx.notes.append(
                    "fail-fast: stopped after assertion {} ({})".format(
                        position, assertion.kind
                    )
                )
                break
        return False

    def _register_dom_queries(
        self, scenario: ScenarioDecl, ctx: AssertionContext
    ) -> Dict[int, int]:
        """Pre-register every ``dom`` assertion's query in the browser program.

        Returns ``{assertion position -> query index}``. Registration happens
        before the program runs so all queries share the one browser session the
        actions already drove — re-launching a browser per assertion would throw
        away the state those actions established.
        """
        mapping: Dict[int, int] = {}
        if ctx.browser is None:
            return mapping
        for position, assertion in enumerate(scenario.assertions):
            if assertion.kind == "dom":
                mapping[position] = ctx.browser.add_query(assertion.params)
        return mapping

    # ------------------------------------------------------------------
    # budget
    # ------------------------------------------------------------------

    def _exhausted(self, ctx: AssertionContext) -> bool:
        remaining = ctx.remaining()
        return remaining is not None and remaining <= 0


def _argv_of(value: Any) -> List[str]:
    """Normalise a declared command to argv, shelling out for a bare string."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(part) for part in value]
    text = str(value).strip()
    if not text:
        return []
    # A string command keeps its shell: scenario authors write pipelines and
    # redirections, and splitting on whitespace here would silently break them.
    return ["sh", "-lc", text]


def _slug(text: str) -> str:
    cleaned = "".join(
        char if char.isalnum() or char in "-_." else "-" for char in (text or "")
    ).strip("-")
    return cleaned or "item"


def run_scenarios(
    executor: Executor,
    scenarios: Sequence[ScenarioDecl],
    *,
    handle: Optional[EnvironmentHandle] = None,
) -> List[ScenarioResult]:
    """Run several scenarios in declaration order, collecting every result."""
    return [executor.run_scenario(scenario, handle=handle) for scenario in scenarios]
