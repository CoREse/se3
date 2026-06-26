"""Shared pytest fixtures for the se3 test suite.

Clears every ``_warned_*_for`` dedup set in ``se3.config`` between tests
so that warning-related caplog assertions are order-independent, regardless
of which warnings a given test happens to trigger.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _wire_browser_test_libs() -> None:
    """Make userspace-installed browser system libraries discoverable.

    The headless-browser acceptance test launches a real Chromium, which needs
    a set of system shared libraries (``libnspr4``, ``libnss3``, ``libgbm1`` …).
    On hosts without those packages installed system-wide and without root,
    ``scripts/install_browser_test_libs.sh`` extracts them into
    ``.browser-libs/lib`` (gitignored). Chromium is launched as a child process
    and reads ``LD_LIBRARY_PATH`` at exec time, so prepending that directory to
    the current process environment is enough for the child to find the libs.

    No-op when the directory does not exist (e.g. the libs are already present
    system-wide, or the test environment expects a loud failure with install
    guidance).
    """
    lib_dir = Path(__file__).parent.parent / ".browser-libs" / "lib"
    if not lib_dir.is_dir():
        return
    existing = os.environ.get("LD_LIBRARY_PATH", "")
    parts = existing.split(os.pathsep) if existing else []
    if str(lib_dir) not in parts:
        os.environ["LD_LIBRARY_PATH"] = os.pathsep.join([str(lib_dir), *parts])


_wire_browser_test_libs()

import se3.config as _cfg  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_config_warning_dedup_sets():
    """Clear all ``_warned_*_for`` sets in se3.config around each test.

    Discovered dynamically so newly added dedup sets are covered without
    having to touch this fixture.
    """

    def _clear_all():
        for name in dir(_cfg):
            if name.startswith("_warned_") and name.endswith("_for"):
                obj = getattr(_cfg, name)
                if isinstance(obj, set):
                    obj.clear()

    _clear_all()
    yield
    _clear_all()


@pytest.fixture(autouse=True)
def _no_real_code_index_refresh(monkeypatch):
    """Neutralise the flow-step code-index freshness hook for every unit test.

    Two step handlers (``analyze`` read-side, ``commit`` write-side) call
    ``context_builder.ensure_code_index_fresh(project_root)`` to lazily rebuild
    ``se3/code-index.md``. In tests a ``FlowInstance`` usually has no
    ``change_path``, so ``project_root`` falls back to ``Path.cwd()`` — the real
    se3 repo, which now ships a committed ``se3/code-index.md``. The hook's
    "no map yet → skip" guard then no longer fires, and it runs a *real*
    incremental build against the live repo: it takes an exclusive ``flock``
    (so concurrent test processes deadlock on it) and spawns a real LLM
    summariser subprocess for any stale file. That is exactly what hung the
    suite past the 1200s timeout.

    Unit tests must never trigger a real code-index build, so the hook is a
    no-op by default. The dedicated code_index tests exercise the builder
    directly via ``build_index`` / ``load_or_build`` (not this hook) and are
    unaffected; a test that specifically wants the real hook can re-patch it.
    """
    monkeypatch.setattr(
        "se3.engine.context_builder.ensure_code_index_fresh",
        lambda *args, **kwargs: None,
    )
