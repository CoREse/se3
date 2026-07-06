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


# Roots that must never be written into by a test's chat-history call. A step
# handler that resolves ``project_root`` to ``flow.change_path.parent`` when the
# flow has no ``change_path`` falls back to ``Path.cwd()`` — the live se3 repo —
# so ``_record_test_history`` (test step) and any other same-suite caller would
# otherwise append fake conversation jsonl into the real ``se3/history/``. The
# repo root is derived from this file's location (``tests/`` sits directly under
# it) and ``Path.cwd()`` is included too since that is exactly the fallback path.
def _real_history_roots() -> set:
    roots = set()
    try:
        roots.add(Path(__file__).resolve().parent.parent)
    except (OSError, IndexError):
        pass
    try:
        roots.add(Path.cwd().resolve())
    except OSError:
        pass
    return roots


def _install_chat_history_guard(monkeypatch, real_roots: set) -> None:
    """Wrap ``chat_history._append_message`` so writes to the live repo no-op.

    ``_append_message`` is the single disk-write point behind ``record_prompt`` /
    ``record_response``. When a test's resolved ``project_root`` equals a real
    repo root we drop the write (tests must never leak fake history into the
    committed ``se3/history/``); a ``project_root`` under a tmp dir passes
    straight through, so ``tests/test_chat_history.py`` and any tmp-scoped caller
    keep working, and production (no fixture installed) is untouched.
    """
    from se3.engine import chat_history

    original = chat_history._append_message

    def _guarded(project_root, flow_id, step_id, msg):
        try:
            target = Path(project_root).resolve()
        except (TypeError, ValueError, OSError):
            target = None
        if target is not None and target in real_roots:
            return
        return original(project_root, flow_id, step_id, msg)

    monkeypatch.setattr(chat_history, "_append_message", _guarded)


@pytest.fixture(autouse=True)
def _no_chat_history_leak_to_real_repo(monkeypatch):
    """Neutralise chat-history writes aimed at the live repo for every test.

    Twin of the fixture in ``src/se3/engine/conftest.py`` (kept in sync). See
    :func:`_install_chat_history_guard` for the rationale.
    """
    _install_chat_history_guard(monkeypatch, _real_history_roots())


@pytest.fixture(autouse=True)
def _isolate_se3_daemon_home(tmp_path, monkeypatch):
    """Redirect the daemon's SE3 home (``~/.se3``) to a per-test tmp dir.

    ``_default_pid_dir()`` honours ``SE3_DAEMON_DIR``; pointing it at a tmp dir
    means any code that touches the pidfile / status file / ``project_roots.json``
    — including daemon discovery tests and the real ``se3`` subprocesses they
    spawn (which inherit this env var) — can never read or persist into the real
    ``~/.se3/``. A test that manages ``SE3_DAEMON_DIR`` itself still overrides
    this via its own ``monkeypatch`` (same instance, applied after this setup).
    """
    monkeypatch.setenv("SE3_DAEMON_DIR", str(tmp_path / ".se3-daemon-home"))
