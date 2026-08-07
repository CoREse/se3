"""``luo e2e`` — the manual operator surface for the e2e subsystem.

Four subcommands, all of them thin shells around the *same* code the flow
engine's ``E2E`` step runs:

``run``        :func:`tianluo.e2e.session.run_e2e`
``list``       :func:`tianluo.e2e.content_config.load_content_config`
``doctor``     :func:`tianluo.e2e.runtime_probe.probe_one` — the preflight itself
``bootstrap``  :mod:`tianluo.e2e.bootstrap`

INVARIANT: there is no second execution path here. A scenario that passes under
``luo e2e run`` and fails inside a flow (or the reverse) would make the command
worse than useless for debugging, so this module owns rendering and exit codes
and nothing else.

Exit codes (scripted callers can tell the three outcomes apart)::

    0  everything passed / the host is fine
    1  at least one e2e scenario failed — a defect in the code under test
    3  environment problem: no usable container runtime, or no permission
    4  configuration problem: content missing or not admissible

WHY 3 and 4 rather than 2: Click reserves exit code 2 for its own usage errors
(a bad option, a missing argument), and a script distinguishing "you typed the
command wrong" from "this host cannot run containers" needs those to stay apart.

Every ``tianluo.e2e`` import sits inside a function body. This module is part of
the CLI's command tree, which a core-only install builds on every ``luo``
invocation; a module-level import would put the e2e stack — and anything behind
the ``tianluo[e2e]`` extra — on that path for every project, e2e or not.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, List, Optional

import typer

from ..i18n import t

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_SCENARIO_FAILED = 1
EXIT_ENVIRONMENT = 3
EXIT_CONFIG = 4

# Failed scenarios printed in full before the list is cut short. A fix loop
# usually needs the first few; the complete record is in the step output and the
# artifacts directory.
_MAX_FAILED_SHOWN = 10

e2e_app = typer.Typer(
    name="e2e",
    help=t("cli.help.e2e"),
)


def _resolve_root(project_root: Optional[str]) -> Path:
    """Resolve the project root and bind the UI language to it."""
    if project_root:
        from ..i18n import bind_project_root

        root = Path(project_root)
        # get_project_root() binds the language itself; an explicit
        # --project-root bypasses it, so bind here too.
        bind_project_root(root)
        return root

    from .run import get_project_root

    return get_project_root()


def _note_if_disabled(config: Any) -> None:
    """Mention that the flow-side switch is off, without refusing to run.

    ``e2e.enabled`` governs whether the *state machine* inserts the E2E step. A
    person typing ``luo e2e run`` has already expressed the intent that switch
    encodes, so blocking them would make it impossible to try e2e out before
    committing the project to it. The notice keeps the distinction visible.
    """
    if not getattr(config, "enabled", False):
        typer.echo(t("cli.e2e.disabled"))


def _print_environment_error(message: str, remediation: str) -> None:
    typer.echo(t("cli.e2e.doctor_runtime_unavailable", detail=message), err=True)
    if remediation:
        typer.echo(remediation, err=True)


@e2e_app.command(name="run", help=t("cli.e2e.help.run"))
def run_command(
    scenario: Optional[List[str]] = typer.Option(
        None, "--scenario", "-s", help=t("cli.e2e.option.scenario")
    ),
    keep: bool = typer.Option(False, "--keep", help=t("cli.e2e.option.keep")),
    write_baselines: bool = typer.Option(
        False, "--write-baselines", help=t("cli.e2e.option.write_baselines")
    ),
    project_root: Optional[str] = typer.Option(
        None, "--project-root", "-p", help=t("cli.help.common.project_root")
    ),
) -> None:
    """Run the project's e2e scenarios in a real isolated environment.

    Exits 1 when a scenario failed, 3 when the host cannot run containers and 4
    when the content configuration is missing or inadmissible.

    Examples:
        luo e2e run
        luo e2e run --scenario cli-smoke --scenario api-smoke
        luo e2e run --keep
    """
    root = _resolve_root(project_root)

    from ..config import E2EConfig
    from ..e2e.content_config import content_relpath, load_content_config
    from ..e2e.errors import E2EConfigError, E2EEnvironmentError
    from ..e2e.runtime_probe import preflight
    from ..e2e.session import run_e2e
    from ..runtime_paths import runtime_dir

    config = E2EConfig.load(root)
    _note_if_disabled(config)

    selected = [name for name in (scenario or []) if name]

    # Preflight up front rather than letting run_e2e do it, purely so the
    # "running N scenarios on <runtime>" line can name the runtime before the
    # slow part starts. The probe result is handed to run_e2e, which therefore
    # does not probe twice — same logic, one execution.
    try:
        probe_result = preflight(config)
        content = load_content_config(root)
    except E2EEnvironmentError as exc:
        _print_environment_error(exc.message, exc.remediation)
        raise typer.Exit(EXIT_ENVIRONMENT)
    except E2EConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_CONFIG)

    if content is None:
        typer.echo(
            t("cli.e2e.no_content", directory=str(content_relpath(root))), err=True
        )
        raise typer.Exit(EXIT_CONFIG)

    planned = selected or list(config.scenarios) or [s.name for s in content.scenarios]
    typer.echo(
        t("cli.e2e.running", count=len(planned), runtime=probe_result.name)
    )

    artifacts = runtime_dir(root) / "logs" / "e2e" / "manual"
    try:
        verdict = run_e2e(
            root,
            scenarios=selected or None,
            config=config,
            content=content,
            probe=probe_result,
            # None means "let the config decide"; the flag can only turn keeping
            # on, never off, so a configured keep_environment still holds.
            keep_environment=True if keep else None,
            write_missing_baselines=write_baselines,
            artifacts_dir=artifacts,
        )
    except E2EEnvironmentError as exc:
        _print_environment_error(exc.message, exc.remediation)
        raise typer.Exit(EXIT_ENVIRONMENT)
    except E2EConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_CONFIG)

    if verdict.environment_error:
        _print_environment_error(verdict.environment_error, verdict.remediation)
        raise typer.Exit(EXIT_ENVIRONMENT)

    summary = verdict.summary or {}
    if verdict.passed:
        typer.echo(t("cli.e2e.result_passed", count=summary.get("total", 0)))
        _print_artifacts(summary, artifacts)
        raise typer.Exit(EXIT_OK)

    typer.echo(
        t(
            "cli.e2e.result_failed",
            failed=summary.get("failed", len(verdict.failed_scenarios)),
            total=summary.get("total", 0),
        ),
        err=True,
    )
    for result in verdict.failed_scenarios[:_MAX_FAILED_SHOWN]:
        typer.echo("  " + result.summary_line(), err=True)
    remaining = len(verdict.failed_scenarios) - _MAX_FAILED_SHOWN
    if remaining > 0:
        typer.echo(
            "  " + t("cli.steprender.e2e.more_scenarios", count=remaining), err=True
        )
    _print_artifacts(summary, artifacts)
    raise typer.Exit(EXIT_SCENARIO_FAILED)


def _print_artifacts(summary: Any, artifacts: Path) -> None:
    if summary and summary.get("artifacts"):
        typer.echo(t("cli.e2e.artifacts_dir", directory=str(artifacts)))


@e2e_app.command(name="list", help=t("cli.e2e.help.list"))
def list_command(
    project_root: Optional[str] = typer.Option(
        None, "--project-root", "-p", help=t("cli.help.common.project_root")
    ),
) -> None:
    """List the declared e2e services and scenarios.

    Reads and validates the content configuration but builds nothing, so it is
    safe on a host with no container runtime at all.
    """
    root = _resolve_root(project_root)

    from ..config import E2EConfig
    from ..e2e.content_config import content_relpath, load_content_config
    from ..e2e.errors import E2EConfigError

    _note_if_disabled(E2EConfig.load(root))

    try:
        content = load_content_config(root)
    except E2EConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_CONFIG)

    if content is None:
        typer.echo(
            t("cli.e2e.no_content", directory=str(content_relpath(root))), err=True
        )
        raise typer.Exit(EXIT_CONFIG)

    typer.echo(t("cli.e2e.services_header", count=len(content.services)))
    for service in content.services:
        typer.echo(
            t(
                "cli.e2e.service_line",
                name=service.name,
                image=service.image,
                base_kind=service.base_kind,
            )
        )

    typer.echo("")
    typer.echo(t("cli.e2e.declared_header", count=len(content.scenarios)))
    for declared in content.scenarios:
        typer.echo(
            t(
                "cli.e2e.scenario_line",
                name=declared.name,
                driver=declared.driver,
                assertions=len(declared.assertions),
            )
        )
    raise typer.Exit(EXIT_OK)


@e2e_app.command(name="doctor", help=t("cli.e2e.help.doctor"))
def doctor_command(
    project_root: Optional[str] = typer.Option(
        None, "--project-root", "-p", help=t("cli.help.common.project_root")
    ),
) -> None:
    """Check whether this host can run e2e, and say how to fix it if not.

    Runs the *same* ``docker info`` / ``podman info`` probe the E2E step's
    preflight runs, so a green doctor and a red preflight cannot disagree. It
    creates no network, builds no image and starts no container.

    Under ``runtime: auto`` every candidate is probed and reported even after one
    succeeds — selection short-circuits, but a diagnostic that hid the second
    runtime's state would be answering a different question. An explicitly
    configured runtime is probed alone: e2e never silently falls back to the
    other one, so reporting on it would be misleading.

    Exits 3 when no usable runtime was found.
    """
    root = _resolve_root(project_root)

    from ..config import E2EConfig
    from ..e2e.errors import E2EConfigError
    from ..e2e.runtime_probe import (
        RUNTIME_AUTO,
        SUPPORTED_RUNTIMES,
        normalize_preference,
        probe_one,
    )

    config = E2EConfig.load(root)

    typer.echo(t("cli.e2e.doctor_header"))
    typer.echo(t("cli.e2e.doctor_config", preference=config.runtime))

    try:
        preference = normalize_preference(config.runtime)
    except E2EConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_CONFIG)

    candidates = (
        SUPPORTED_RUNTIMES if preference == RUNTIME_AUTO else (preference,)
    )

    selected = None
    remediation = ""
    for name in candidates:
        result = probe_one(name)
        if result.ok:
            typer.echo(
                t(
                    "cli.e2e.doctor_runtime_ok",
                    runtime=result.name,
                    binary=result.binary,
                    rootless=result.rootless,
                )
            )
            if selected is None:
                selected = result
        else:
            typer.echo(
                t("cli.e2e.doctor_runtime_unavailable", detail=result.error), err=True
            )
            remediation = remediation or result.remediation

    if selected is not None:
        typer.echo(t("cli.e2e.doctor_selected", runtime=selected.name))
        raise typer.Exit(EXIT_OK)

    if remediation:
        typer.echo(remediation, err=True)
    raise typer.Exit(EXIT_ENVIRONMENT)


@e2e_app.command(name="bootstrap", help=t("cli.e2e.help.bootstrap"))
def bootstrap_command(
    hint: Optional[List[str]] = typer.Option(
        None, "--hint", help=t("cli.e2e.option.hint")
    ),
    project_root: Optional[str] = typer.Option(
        None, "--project-root", "-p", help=t("cli.help.common.project_root")
    ),
) -> None:
    """Generate or evolve the project's ``tianluo/e2e/`` content configuration.

    With no ``--hint`` this is first-time generation and does nothing when the
    directory is already complete. With one or more ``--hint`` values it evolves
    the existing content incrementally — new or revised scenarios only; nothing
    is rewritten wholesale and nothing is deleted.

    Never writes ``tianluo.yaml``: the ``e2e.enabled`` switch stays yours.

    Examples:
        luo e2e bootstrap
        luo e2e bootstrap --hint "the /健康 endpoint moved to /health"
    """
    root = _resolve_root(project_root)

    from ..e2e import bootstrap as bootstrap_module
    from ..e2e.content_config import content_relpath
    from ..e2e.errors import E2EConfigError

    hints = [value for value in (hint or []) if value]
    directory = str(content_relpath(root))

    try:
        if hints:
            result = bootstrap_module.evolve_content(root, None, hints)
        else:
            result = bootstrap_module.ensure_content(root, None)
    except E2EConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_CONFIG)

    if not result.changed:
        typer.echo(t("cli.e2e.bootstrap_unchanged", directory=directory))
        # An evolution that produced nothing usable still reports why, but is not
        # an error: the suite already on disk is valid and runnable.
        for message in result.errors:
            typer.echo(message, err=True)
        raise typer.Exit(EXIT_OK)

    typer.echo(t("cli.e2e.bootstrap_done", directory=directory))
    for written in result.written:
        typer.echo(t("cli.e2e.written_line", file=written))
    raise typer.Exit(EXIT_OK)


__all__ = [
    "EXIT_CONFIG",
    "EXIT_ENVIRONMENT",
    "EXIT_OK",
    "EXIT_SCENARIO_FAILED",
    "e2e_app",
]
