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


def _install_chat_history_guard(monkeypatch, real_roots: set, redirect_root: Path) -> None:
    """Reroute chat-history reads/writes aimed at the live repo into a tmp dir.

    Every ``record_*`` writer resolves its target path through the module-global
    ``chat_history._history_dir`` (via ``_history_file``) — ``record_prompt`` /
    ``record_response`` (through ``_append_message``) *and* the sibling writers
    ``record_step_event`` / ``record_stream_progress`` /
    ``record_user_interjection`` and every other ``record_*``. Wrapping only
    ``_append_message`` (the old guard) therefore missed those siblings, which
    append straight into ``se3/history/<flow_id>/*.jsonl``. Patching the single
    ``_history_dir`` resolution point instead covers ALL writers at once: when a
    test's resolved ``project_root`` equals a real repo root the path is
    rerouted under ``redirect_root`` (a per-test tmp dir), so nothing can leak
    into the committed ``se3/history/`` through any writer; a ``project_root``
    under a tmp dir passes straight through, so ``tests/test_chat_history.py``
    and any tmp-scoped caller keep working, and production (no fixture
    installed) is untouched.

    ``se3.engine.state_machine`` binds ``_history_dir`` at import time (a
    module-level ``from ... import _history_dir``), so its reference bypasses the
    module-attribute patch above and is repatched directly.
    """
    from se3.engine import chat_history

    original = chat_history._history_dir

    def _redirected(project_root, flow_id):
        try:
            target = Path(project_root).resolve()
        except (TypeError, ValueError, OSError):
            target = None
        if target is not None and target in real_roots:
            return original(redirect_root, flow_id)
        return original(project_root, flow_id)

    monkeypatch.setattr(chat_history, "_history_dir", _redirected)
    try:
        from se3.engine import state_machine

        monkeypatch.setattr(
            state_machine, "_history_dir", _redirected, raising=False
        )
    except Exception:  # pragma: no cover - defensive
        pass


@pytest.fixture(autouse=True)
def _no_chat_history_leak_to_real_repo(tmp_path, monkeypatch):
    """Neutralise chat-history writes aimed at the live repo for every test.

    Twin of the fixture in ``src/se3/engine/conftest.py`` (kept in sync). See
    :func:`_install_chat_history_guard` for the rationale.
    """
    _install_chat_history_guard(
        monkeypatch, _real_history_roots(), tmp_path / "_chat_history_redirect"
    )


@pytest.fixture(autouse=True)
def _isolate_se3_daemon_home(tmp_path, monkeypatch):
    """Redirect the daemon's SE3 home (``~/.se3``) and hide test stubs from scans.

    Two suite-wide isolations, both to keep tests off the real ``~/.se3/``:

    * ``SE3_DAEMON_DIR`` → a per-test tmp dir. ``_default_pid_dir()`` honours it,
      so any code that touches the pidfile / status file / ``project_roots.json``
      — including daemon discovery tests and the real ``se3`` subprocesses they
      spawn (which inherit this env var) — can never read or persist into the
      real ``~/.se3/``. A test that manages ``SE3_DAEMON_DIR`` itself still
      overrides this via its own ``monkeypatch`` (same instance, applied after).
    * ``SE3_EXTERNAL_SCAN_IGNORE`` = "1" — inherited by every subprocess a test
      spawns. A *separate* real daemon already running on this machine does not
      inherit ``SE3_DAEMON_DIR`` (a different process tree), so its ``psutil``
      scan would otherwise discover a test's fake ``se3 run`` stub and persist
      its pytest-tempdir cwd into the real registry. The marker makes those
      stubs invisible to a *current* daemon's external scan (see
      ``supervisor.EXTERNAL_SCAN_IGNORE_ENV``); the one test that must still
      discover its own stub opts back in via ``include_scan_ignored=True``.

    The marker is honoured only by a daemon carrying this fix. A **pre-fix**
    daemon predating ``EXTERNAL_SCAN_IGNORE_ENV`` ignores the marker and still
    recognises a ``[.../se3, run]`` stub as a genuine flow. This isolation
    therefore protects only against a **fixed** daemon (and against the test's
    own ``SE3_DAEMON_DIR``-scoped subprocesses). It does NOT claim to defend
    against a still-running pre-fix daemon: some discovery tests deliberately
    spawn ``se3 run``-shaped stubs with cwd under a pytest tempdir — e.g.
    ``test_discovers_live_worktree_*`` in ``tests/test_end_session_cmd.py`` run
    with ``cwd=tmp_path/"repo"`` — so a pre-fix daemon that grabs such a stub
    can re-register a ``/tmp/pytest-of-*`` tempdir into the real registry. That
    scenario is handled out-of-band, per the accepted task scope, by restarting
    (or stopping) the resident daemon onto fixed code and re-running
    ``scripts/cleanup_project_roots.py`` to prune any pytest-tempdir residue —
    not by anything this fixture can do.
    """
    from se3.daemon.supervisor import EXTERNAL_SCAN_IGNORE_ENV

    monkeypatch.setenv("SE3_DAEMON_DIR", str(tmp_path / ".se3-daemon-home"))
    monkeypatch.setenv(EXTERNAL_SCAN_IGNORE_ENV, "1")
