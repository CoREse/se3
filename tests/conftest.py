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
