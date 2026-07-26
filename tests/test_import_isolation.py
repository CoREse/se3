"""Import-isolation tests for the ``tianluo[server]`` optional extra.

These tests verify the dependency-isolation hard constraint of the
daemon/server feature: a core-only ``pip install se3`` (without the
``[server]`` extra) must keep the core CLI fully functional and free of
``ImportError``, and importing core modules must never drag in the heavy web
dependencies (``fastapi`` / ``uvicorn``).

Each check runs in a *fresh* Python subprocess so that modules already
imported by the pytest session do not mask a leaked import.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# The worktree's src/ must be importable in the fresh subprocesses these
# tests spawn: pytest's `pythonpath = ["src"]` applies only to the test
# session itself, and relying on an *installed* distribution would silently
# test stale code (or, post-rename, no `tianluo` at all).
_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")


def _subprocess_env() -> dict:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = _SRC_DIR + (os.pathsep + existing if existing else "")
    return env

# Core modules that a core-only install MUST be able to import without the
# server extra present.
CORE_MODULES = [
    "se3",
    "tianluo.cli",
    "tianluo.commands",
    "tianluo.commands.run",
    "tianluo.engine",
    "tianluo.engine.state_machine",
    "tianluo.daemon",
    "tianluo.daemon.daemon",
]

# `se3` command-family subcommands whose `--help` must not trigger a server
# import (or any ImportError) when the extra is absent.
CORE_HELP_COMMANDS = ["run", "merge", "history", "issue", "daemon"]


def _run_python(code: str) -> subprocess.CompletedProcess:
    """Run ``code`` in a fresh interpreter and return the completed process."""
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        env=_subprocess_env(),
    )


def test_core_modules_import_without_server_modules_leaking() -> None:
    """Importing every core module must not pull in fastapi/uvicorn."""
    code = f"""
        import sys
        modules = {CORE_MODULES!r}
        for name in modules:
            __import__(name)
        leaked = sorted(
            m for m in sys.modules
            if m == "fastapi" or m == "uvicorn"
            or m.startswith("fastapi.") or m.startswith("uvicorn.")
        )
        assert not leaked, "server deps leaked into core import: " + repr(leaked)
        print("OK")
    """
    proc = _run_python(code)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "OK" in proc.stdout


def test_importing_se3_does_not_import_server_package() -> None:
    """`import tianluo` (and the CLI) must not import the `tianluo.server` package."""
    code = """
        import sys
        import tianluo
        import tianluo.cli  # building the Typer command tree
        leaked = [m for m in sys.modules if m == "tianluo.server"
                  or m.startswith("tianluo.server.")]
        assert not leaked, "tianluo.server leaked into core import: " + repr(leaked)
        print("OK")
    """
    proc = _run_python(code)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "OK" in proc.stdout


@pytest.mark.parametrize("command", CORE_HELP_COMMANDS)
def test_core_command_help_has_no_import_error(command: str) -> None:
    """`se3 <command> --help` must succeed and never load the server extra."""
    proc = subprocess.run(
        [sys.executable, "-m", "tianluo.cli", command, "--help"],
        capture_output=True,
        text=True,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0, (
        f"`se3 {command} --help` failed: "
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "ImportError" not in proc.stderr
    assert "ModuleNotFoundError" not in proc.stderr


def test_se3_server_reports_clear_hint_when_extra_missing() -> None:
    """`se3-server` must print an install hint (not a traceback) when the
    fastapi/uvicorn extra is unavailable."""
    # Simulate a core-only install by making fastapi/uvicorn unimportable.
    code = """
        import builtins, sys
        _real_import = builtins.__import__

        def _blocked(name, *args, **kwargs):
            top = name.split(".")[0]
            if top in ("fastapi", "uvicorn"):
                raise ImportError("simulated missing server extra: " + name)
            return _real_import(name, *args, **kwargs)

        builtins.__import__ = _blocked
        from tianluo.server import main
        try:
            main([])
        except SystemExit as exc:
            print("EXITCODE", exc.code)
    """
    proc = _run_python(code)
    # The friendly hint goes to stderr; exit code is non-zero (1).
    assert "EXITCODE 1" in proc.stdout, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "pip install" in proc.stderr
    assert "tianluo[server]" in proc.stderr
    # No raw traceback should reach the user.
    assert "Traceback (most recent call last)" not in proc.stderr
