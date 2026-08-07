"""tianluo.e2e — the end-to-end testing subsystem.

e2e is a *general* tianluo capability: it applies to every managed project
regardless of shape (web service, CLI tool, desktop GUI). It is off by default
and turns on only when the project's ``tianluo.yaml`` carries ``e2e.enabled:
true`` — a switch the user owns, because enabling it asserts that Docker or
Podman is installed and that the fix loop may spend time running scenarios.

Layering (bottom-up)::

    errors          exception taxonomy (environment vs. scenario failure)
    backend         IsolationBackend narrow ABC + data contracts
    runtime_probe   execution-based docker/podman detection, doubles as preflight

WHY: this ``__init__`` deliberately declares the package and nothing else — it
imports no submodule, no container backend, no assertion implementation, and
above all no third-party dependency from the ``tianluo[e2e]`` optional extra.
It is the guard point for the charter's dependency-isolation constraint: a
core-only ``pip install tianluo`` must be able to ``import tianluo.e2e``
(the CLI's command tree and the engine's step registry both reach this package)
without the extra present. Eager re-exports here would drag Pillow / the
container layer into every core import and turn a missing extra into an
``ImportError`` on unrelated commands. Consumers import the submodule they
need, and the extra-backed modules check their dependency at call time and
raise :class:`~tianluo.e2e.errors.E2EDependencyMissingError` with an install
hint instead.

The framework code and Dockerfile templates themselves ship in full with the
wheel — the extra isolates *third-party dependencies*, not tianluo's own code.
"""

from __future__ import annotations

__all__ = [
    "backend",
    "errors",
    "runtime_probe",
]
