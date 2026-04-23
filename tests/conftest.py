"""Shared pytest fixtures for the se3 test suite.

Clears every ``_warned_*_for`` dedup set in ``se3.config`` between tests
so that warning-related caplog assertions are order-independent, regardless
of which warnings a given test happens to trigger.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

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
