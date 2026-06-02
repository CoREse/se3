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
    "se3-server requires the optional 'server' extra.\n"
    "Install it with:\n"
    "    pip install 'se3[server]'\n"
)


def main(argv: Optional[list] = None) -> None:
    """``se3-server`` console-script entry point.

    Verifies the ``server`` extra (FastAPI / uvicorn) is importable, then
    delegates to :func:`se3.server.app.main`. When the extra is missing it
    prints an install hint and exits non-zero — no traceback, and the core
    ``se3`` CLI is unaffected because it never calls this function.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if "--version" in args or "-v" in args:
        from se3 import __version__

        print(f"se3-server version {__version__}")
        raise SystemExit(0)

    # Break-glass token issuance is the fail-closed escape hatch: it must work
    # even on a core-only install (no FastAPI / uvicorn) and even when the auth
    # provider is broken. Intercept it *before* the [server]-extra check so the
    # CLI only touches the stdlib-sqlite persistence layer.
    if args and args[0] == "bootstrap-token":
        from .bootstrap import run_bootstrap_token_cli

        raise SystemExit(run_bootstrap_token_cli(args[1:]))

    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        sys.stderr.write(_MISSING_EXTRA_HINT)
        raise SystemExit(1)

    from .app import main as _app_main

    _app_main(argv)
