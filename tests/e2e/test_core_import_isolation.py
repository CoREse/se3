"""Regression guard for the charter's *core dependency isolation* constraint.

The e2e subsystem's heavy third-party pieces live in the ``tianluo[e2e]``
optional extra. Two things must therefore hold forever:

1. A core-only install must be able to import the CLI, the engine's step
   registry and ``tianluo.e2e`` itself without the extra present — no
   ``ImportError``, no ``ModuleNotFoundError``.
2. Those same imports must not *drag in* the extra's dependencies (Pillow) even
   when they happen to be installed, because a lazy import that quietly becomes
   eager is invisible until it reaches a user who never asked for e2e.

The first four checks run in a *fresh* interpreter, so a module the pytest
session already imported cannot mask a leak. The last one exercises the
blocking hook in-process purely to prove it restores ``sys.meta_path``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from contextlib import contextmanager
from pathlib import Path

# The worktree's src/ must be importable in the subprocesses spawned below:
# pytest's `pythonpath = ["src"]` covers only the test session itself, and
# falling back to an installed distribution would silently test stale code.
_SRC_DIR = str(Path(__file__).resolve().parents[2] / "src")

# The extra-only dependency the e2e assertion layer imports lazily (baseline
# screenshot diffing). Listed by *top-level* module name, which is what a
# meta-path finder matches on.
EXTRA_TOP_LEVEL_MODULES = ["PIL"]

# Modules a core-only install must be able to import with the extra absent.
CORE_MODULES = [
    "tianluo.cli",
    "tianluo.engine.steps",
    "tianluo.e2e",
    "tianluo.e2e.errors",
    "tianluo.e2e.backend",
    "tianluo.e2e.runtime_probe",
    "tianluo.e2e.config_schema",
    "tianluo.e2e.content_config",
]

# Source of the meta-path hook that simulates a machine where the e2e extra was
# never installed. Shared verbatim by the subprocess checks below.
_BLOCKER_SRC = """
    import sys

    class _ExtraBlocker:
        def __init__(self, blocked):
            self.blocked = set(blocked)

        def find_spec(self, fullname, path=None, target=None):
            if fullname.split(".")[0] in self.blocked:
                raise ModuleNotFoundError(
                    "simulated missing e2e extra: " + fullname, name=fullname
                )
            return None

    _blocker = _ExtraBlocker({blocked!r})
    for _name in list(sys.modules):
        if _name.split(".")[0] in _blocker.blocked:
            del sys.modules[_name]
    sys.meta_path.insert(0, _blocker)
"""


def _subprocess_env() -> dict:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = _SRC_DIR + (os.pathsep + existing if existing else "")
    return env


def _run_python(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        env=_subprocess_env(),
    )


def test_core_imports_do_not_pull_in_the_e2e_extra() -> None:
    """Importing the CLI / engine steps / ``tianluo.e2e`` must not import Pillow.

    Asserts "was not imported", not "is unavailable" — the check is meaningful
    precisely on a machine (like CI) where Pillow *is* installed.
    """
    code = f"""
        import sys
        for name in {CORE_MODULES!r}:
            __import__(name)
        blocked = {EXTRA_TOP_LEVEL_MODULES!r}
        leaked = sorted(
            m for m in sys.modules
            if m.split(".")[0] in blocked
        )
        assert not leaked, "e2e extra leaked into core import: " + repr(leaked)
        print("OK")
    """
    proc = _run_python(code)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "OK" in proc.stdout


def test_e2e_package_import_pulls_in_no_submodule() -> None:
    """``import tianluo.e2e`` must stay a bare package declaration.

    The package ``__init__`` is the guard point: if it ever re-exports the
    container backend or the assertion layer for convenience, every core-only
    import starts paying for (and can start failing on) the e2e stack.
    """
    code = """
        import sys
        import tianluo.e2e
        eager = sorted(
            m for m in sys.modules
            if m.startswith("tianluo.e2e.")
        )
        assert not eager, "tianluo.e2e.__init__ imported submodules: " + repr(eager)
        print("OK")
    """
    proc = _run_python(code)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "OK" in proc.stdout


def test_core_modules_import_with_the_extra_blocked() -> None:
    """With Pillow made unimportable, the CLI and the engine must still load."""
    code = _BLOCKER_SRC.format(blocked=EXTRA_TOP_LEVEL_MODULES) + f"""
    for name in {CORE_MODULES!r}:
        __import__(name)
    print("OK")
    """
    proc = _run_python(code)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "ModuleNotFoundError" not in proc.stderr
    assert "OK" in proc.stdout


def test_cli_help_works_with_the_extra_blocked() -> None:
    """Building the whole Typer command tree must not need the e2e extra."""
    code = _BLOCKER_SRC.format(blocked=EXTRA_TOP_LEVEL_MODULES) + """
    import tianluo.cli
    from typer.testing import CliRunner

    result = CliRunner().invoke(tianluo.cli.app, ["--help"])
    assert result.exit_code == 0, result.output
    print("OK")
    """
    proc = _run_python(code)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "OK" in proc.stdout


class _ExtraBlocker:
    """In-process twin of the subprocess hook, used only to prove restoration."""

    def __init__(self, blocked) -> None:
        self.blocked = set(blocked)

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in self.blocked:
            raise ModuleNotFoundError(
                f"simulated missing e2e extra: {fullname}", name=fullname
            )
        return None


@contextmanager
def _block_imports(names):
    blocker = _ExtraBlocker(names)
    sys.meta_path.insert(0, blocker)
    try:
        yield blocker
    finally:
        sys.meta_path.remove(blocker)


def test_blocking_hook_is_fully_restored() -> None:
    """The hook must not outlive its test and poison later imports.

    A leaked meta-path entry would make every subsequent test in the same
    process fail on an unrelated import, so restoration is asserted explicitly.
    """
    before = list(sys.meta_path)

    with _block_imports(["tianluo_no_such_package"]) as blocker:
        assert sys.meta_path[0] is blocker
        try:
            import tianluo_no_such_package  # noqa: F401
        except ModuleNotFoundError as exc:
            assert "simulated missing e2e extra" in str(exc)
        else:  # pragma: no cover - the hook always raises
            raise AssertionError("blocking hook did not intercept the import")

    assert sys.meta_path == before
