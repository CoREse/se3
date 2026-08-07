"""tianluo.e2e.errors — the e2e subsystem's exception taxonomy.

The single most important thing this module encodes is the *routing boundary*
between two kinds of e2e failure, because the flow engine treats them in
opposite ways:

* :class:`E2EEnvironmentError` — the *host* cannot run e2e (no container
  runtime, current user lacks permission, optional extra not installed). The
  project's code is innocent, so the E2E step reports ``FAILED`` with a
  remediation hint and the fix loop is never entered.
* :class:`E2EScenarioFailure` — a scenario's assertions did not hold. That is a
  defect in the code under test, so the E2E step reports ``REVISION_NEEDED``
  and the flow enters the ordinary fix loop.

Pure stdlib plus tianluo's own i18n: this module is imported on core-only
installs and must never reach for the ``tianluo[e2e]`` extra.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from tianluo.i18n import t

__all__ = [
    "E2E_EXTRA",
    "E2EConfigError",
    "E2EDependencyMissingError",
    "E2EEnvironmentError",
    "E2EError",
    "E2EScenarioFailure",
]

# The optional-dependency extra that carries e2e's third-party requirements.
E2E_EXTRA = "tianluo[e2e]"


class E2EError(Exception):
    """Base class for every error raised by the e2e subsystem.

    Callers that only need "did e2e blow up?" catch this; callers that route on
    blame (engine step handler, ``luo e2e``) catch the specific subclasses.
    """


class E2EConfigError(E2EError):
    """e2e configuration is malformed or violates the schema.

    Raised by the config/schema layer for a bad ``tianluo.yaml`` ``e2e:`` block
    or a bad ``tianluo/e2e/`` content file — including assertion-ladder
    violations (using a higher assertion tier without declaring it). Messages
    carry the offending file and the YAML path so the author can locate it.
    """


class E2EEnvironmentError(E2EError):
    """The host environment cannot run e2e — the project's code is not at fault.

    WHY: this class is the fix-loop firewall. The engine maps it to a ``FAILED``
    step carrying :attr:`remediation`, never to ``REVISION_NEEDED``. Routing a
    missing/unusable container runtime into the fix loop would dispatch an LLM
    to "repair" the operator's machine and burn the entire
    ``workflow.max_fix_iterations`` budget on something no code change can fix.
    Contrast :class:`E2EScenarioFailure`, which *is* a code defect and does
    enter the fix loop.

    :attr:`remediation` holds actionable, user-facing repair guidance (already
    localized) and is appended to ``str(exc)`` so a bare print still tells the
    user what to do.
    """

    def __init__(self, message: str, *, remediation: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.remediation = remediation or ""

    def __str__(self) -> str:
        if self.remediation:
            return f"{self.message}\n{self.remediation}"
        return self.message


class E2EDependencyMissingError(E2EEnvironmentError):
    """A third-party dependency from the ``tianluo[e2e]`` extra is not installed.

    WHY: subclasses :class:`E2EEnvironmentError` rather than sitting beside it,
    because the blame and the routing are identical — the host is missing a
    package, no code change fixes it, so it must reach ``FAILED`` with a hint
    and not the fix loop. Any handler already catching ``E2EEnvironmentError``
    therefore does the right thing without a second branch.

    The framework's own code and templates ship with the wheel unconditionally;
    only heavy third-party pieces (image diffing) live behind the extra, and
    they are imported lazily so this error is raised — with an actionable
    ``pip install`` line — instead of a bare ``ModuleNotFoundError``.
    """

    def __init__(
        self,
        dependency: str,
        *,
        feature: Optional[str] = None,
        message: Optional[str] = None,
    ) -> None:
        self.dependency = dependency
        self.feature = feature
        if message is None:
            if feature:
                message = t(
                    "e2e.error.dependency_missing_for",
                    dependency=dependency,
                    feature=feature,
                )
            else:
                message = t("e2e.error.dependency_missing", dependency=dependency)
        super().__init__(
            message,
            remediation=t("e2e.remediation.install_extra", extra=E2E_EXTRA),
        )


class E2EScenarioFailure(E2EError):
    """One or more e2e scenarios failed their assertions.

    WHY: this is the *code-defect* half of the taxonomy. The engine maps it to
    ``REVISION_NEEDED`` so the flow enters the ordinary fix loop bounded by
    ``workflow.max_fix_iterations``, exactly like a failing unit test — there is
    deliberately no discard / waiver / severity-based pass-through channel
    (charter: a check step's finding has exactly one destination).

    :attr:`results` carries the structured per-scenario results so the handler
    can build ``fix_instructions`` / ``fix_context`` without re-parsing text.
    """

    def __init__(
        self,
        message: str,
        *,
        scenario: Optional[str] = None,
        results: Optional[Sequence[Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.scenario = scenario
        self.results = list(results) if results else []

    def __str__(self) -> str:
        return self.message
