"""SE3 central server — the multi-machine control-plane backend.

The central server accepts outbound WebSocket connections from any number of
SE3 daemons (``se3 daemon``), aggregates their state, and re-exposes it through
a REST API plus a bundled web frontend. Daemons dial *out* to the server, so
SE3 machines never need an inbound port — the architecture is NAT-friendly.

This is a deliberately *separate* package from the core ``se3`` CLI. Its heavy
web dependencies (``fastapi``, ``uvicorn``) live in the ``se3[server]`` optional
extra. The core ``se3`` command never imports this package, so a core-only
install stays dependency-clean. The server is reached only through the
standalone ``se3-server`` console script, whose :func:`main` is the single
entry point: it verifies the extra is installed *before* importing the FastAPI
app, so a user without the extra gets a clear install hint instead of an
``ImportError`` traceback.
"""

from __future__ import annotations

import sys
from typing import Optional

__all__ = ["main"]

_MISSING_EXTRA_HINT = (
    "tianluo-server requires the optional 'server' extra.\n"
    "Install it with:\n"
    "    pip install 'tianluo[server]'\n"
)


def legacy_main(argv: Optional[list] = None) -> None:
    """Entry point for the deprecated ``se3-server`` console script.

    Prints a one-line migration notice on stderr, then runs the normal
    server entry point. The alias — and this wrapper — are removed in 13.0.0.
    """
    print(
        "se3-server: this command was renamed in 12.0.0 — use `tianluo-server`. "
        "The `se3-server` alias will be removed in 13.0.0.",
        file=sys.stderr,
    )
    main(argv)


def main(argv: Optional[list] = None) -> None:
    """``tianluo-server`` console-script entry point.

    Verifies the ``server`` extra (FastAPI / uvicorn) is importable, then
    delegates to :func:`tianluo.server.app.main`. When the extra is missing it
    prints an install hint and exits non-zero — no traceback, and the core
    ``luo`` CLI is unaffected because it never calls this function.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if "--version" in args or "-v" in args:
        from tianluo import __version__

        print(f"tianluo-server version {__version__}")
        raise SystemExit(0)

    # ``bootstrap-token`` only needs the persistence + crypto layers (stdlib
    # sqlite3), never FastAPI / uvicorn. Intercept it here — like ``--version``
    # — so the break-glass escape hatch can be minted even on a core-only
    # install and stays off the heavy web import chain.
    if args and args[0] == "bootstrap-token":
        from .bootstrap import main as _bootstrap_main

        raise SystemExit(_bootstrap_main(args[1:]))

    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        sys.stderr.write(_MISSING_EXTRA_HINT)
        raise SystemExit(1)

    from .app import main as _app_main

    _app_main(argv)
