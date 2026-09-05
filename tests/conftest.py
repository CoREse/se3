"""Shared pytest fixtures for the se3 test suite.

Clears every ``_warned_*_for`` dedup set in ``tianluo.config`` between tests
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

# WHY: pytest-xdist exports COLUMNS=80 into every worker process, and rich's
# ``Console.__init__`` reads COLUMNS *once* and pins ``_width`` from it. A module
# level ``console = Console()`` (e.g. tianluo.commands.history_cmd) therefore
# freezes at 80 columns in a worker, and a test that widens the terminal for the
# duration of a CLI invocation — ``CliRunner(...).invoke(..., env={"COLUMNS":
# "200"})`` — silently has no effect: rich never re-reads the variable, tables
# clip their columns to "cla…", and content assertions fail under xdist while
# passing serially. Removing the variable before any tianluo module is imported
# leaves ``_width`` at ``None``, which is what the serial run has always had:
# width is then resolved per render, so the per-invocation override works again.
# Any test that genuinely wants a fixed width sets it itself.
os.environ.pop("COLUMNS", None)

# Pin the UI language to en-US at conftest *import* time, before any test module
# is collected. Typer freezes each command/option ``help=`` string when the
# module defining it is imported (the value is a plain ``t(...)`` result bound
# into the Option/command at decoration). Test modules import ``tianluo.cli`` during
# collection — earlier than any autouse fixture can run — so under the repo's own
# ``tianluo.yaml`` (``language: zh-CN``) the help text would freeze in Chinese and
# every English help-text assertion would break. The per-test autouse fixtures
# below re-pin en-US for runtime ``t()`` rendering; this line covers the one-shot
# import-time freeze they cannot reach. Resolution-chain tests clear SE3_LANG via
# their own ``monkeypatch.delenv`` and are unaffected.
os.environ["SE3_LANG"] = "en-US"

import tianluo.config as _cfg  # noqa: E402


@pytest.fixture(autouse=True)
def _force_en_us_ui_language(monkeypatch):
    """Pin the i18n UI language to en-US for every test.

    ``tianluo.i18n`` resolves the active UI language lazily from ``Path.cwd()`` — and
    the suite runs from the repo root, whose ``tianluo.yaml`` sets ``language: zh-CN``.
    Without this, any test that exercises a ``t()``-rendered CLI/display string
    would see Chinese and its English assertion would break, making output
    determinism depend on the repo's own config and the host locale. Forcing
    ``SE3_LANG=en-US`` (the highest-precedence source) plus a singleton reset
    makes rendered text the stable en-US reference for all tests. Tests that
    specifically exercise other languages override this with their own
    ``monkeypatch.setenv``/``set_language`` (applied after this fixture) and a
    reset — e.g. the i18n precedence-chain tests, which ``delenv`` it entirely.
    """
    import tianluo.i18n as _i18n

    monkeypatch.setenv("SE3_LANG", "en-US")
    _i18n.reset_language()
    _i18n.clear_caches()
    yield
    _i18n.reset_language()
    _i18n.clear_caches()


@pytest.fixture(autouse=True)
def _reset_config_warning_dedup_sets():
    """Clear all ``_warned_*_for`` sets in tianluo.config around each test.

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
def _pin_ui_language_en():
    """Pin CLI UI text to en-US so command-output assertions are deterministic.

    ``tianluo.i18n.t()``'s active language is a process-wide singleton resolved from
    ``Path.cwd()``; the se3 repo's own ``tianluo.yaml`` sets ``language: zh-CN``, so
    without an explicit pin the language a test observes would depend on cwd and
    on which test happened to trigger the first render. Pinning to en-US keeps
    every existing English-substring assertion stable regardless of host locale
    or project config. Tests that exercise language switching override this via
    ``i18n.set_language("zh-CN")`` (or ``SE3_LANG`` + ``reset_language()``) in
    their own body; this fixture resets the singleton afterwards so the override
    never leaks. Intentionally does NOT touch ``SE3_LANG``/locale env vars so the
    dedicated resolution-chain tests in ``test_i18n.py`` remain unaffected.
    """
    from tianluo import i18n

    i18n.set_language("en-US")
    yield
    i18n.reset_language()


@pytest.fixture(autouse=True)
def _no_real_code_index_refresh(monkeypatch):
    """Neutralise the flow-step code-index freshness hook for every unit test.

    Two step handlers (``analyze`` read-side, ``commit`` write-side) call
    ``context_builder.ensure_code_index_fresh(project_root)`` to lazily rebuild
    ``tianluo/code-index.md``. In tests a ``FlowInstance`` usually has no
    ``change_path``, so ``project_root`` falls back to ``Path.cwd()`` — the real
    se3 repo, which now ships a committed ``tianluo/code-index.md``. The hook's
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
        "tianluo.engine.context_builder.ensure_code_index_fresh",
        lambda *args, **kwargs: None,
    )


# Roots that must never be written into by a test's chat-history call. A step
# handler that resolves ``project_root`` to ``flow.change_path.parent`` when the
# flow has no ``change_path`` falls back to ``Path.cwd()`` — the live se3 repo —
# so ``_record_test_history`` (test step) and any other same-suite caller would
# otherwise append fake conversation jsonl into the real ``tianluo/history/``. The
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
    append straight into ``tianluo/history/<flow_id>/*.jsonl``. Patching the single
    ``_history_dir`` resolution point instead covers ALL writers at once: when a
    test's resolved ``project_root`` equals a real repo root the path is
    rerouted under ``redirect_root`` (a per-test tmp dir), so nothing can leak
    into the committed ``tianluo/history/`` through any writer; a ``project_root``
    under a tmp dir passes straight through, so ``tests/test_chat_history.py``
    and any tmp-scoped caller keep working, and production (no fixture
    installed) is untouched.

    ``tianluo.engine.state_machine`` binds ``_history_dir`` at import time (a
    module-level ``from ... import _history_dir``), so its reference bypasses the
    module-attribute patch above and is repatched directly.
    """
    from tianluo.engine import chat_history

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
        from tianluo.engine import state_machine

        monkeypatch.setattr(
            state_machine, "_history_dir", _redirected, raising=False
        )
    except Exception:  # pragma: no cover - defensive
        pass


@pytest.fixture(autouse=True)
def _no_chat_history_leak_to_real_repo(tmp_path, monkeypatch):
    """Neutralise chat-history writes aimed at the live repo for every test.

    Twin of the fixture in ``src/tianluo/engine/conftest.py`` (kept in sync). See
    :func:`_install_chat_history_guard` for the rationale.
    """
    _install_chat_history_guard(
        monkeypatch, _real_history_roots(), tmp_path / "_chat_history_redirect"
    )


@pytest.fixture(autouse=True)
def _reset_stdin_funnel():
    """Clear the process-wide non-TTY stdin funnel between tests.

    The funnel is deliberately long-lived (it owns the fd for the whole run),
    so a test that drives it — or that merely trips its feeder on pytest's
    unreadable captured stdin and latches EOF — would otherwise answer every
    later test in the same worker from stale state.
    """
    from tianluo import stdin_channel

    stdin_channel.reset()
    yield
    stdin_channel.reset()


@pytest.fixture(autouse=True)
def _reset_active_interjection_flow(monkeypatch):
    """Unbind the process-wide interjection flow scope between tests.

    ``luo run`` binds it once per process, so production never needs to unbind
    — but a test that drives the run loop leaves the binding set for every
    later test in the same worker, where an unrelated drain would then find
    nothing (the queued call is addressed to some other flow id).
    """
    from tianluo.engine import interaction_calls

    monkeypatch.setattr(interaction_calls, "_active_flow_id", "")


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
    from tianluo.daemon.supervisor import EXTERNAL_SCAN_IGNORE_ENV

    monkeypatch.setenv("SE3_DAEMON_DIR", str(tmp_path / ".se3-daemon-home"))
    monkeypatch.setenv(EXTERNAL_SCAN_IGNORE_ENV, "1")


# ---------------------------------------------------------------------------
# Lethal-signal guard
# ---------------------------------------------------------------------------
# WHY this exists: on 2026-09-02/03 the suite took the whole machine down three
# times. A runner test drove ``_run_single_with_monitor`` with a
# ``MagicMock()`` process; ``int(MagicMock().pid)`` is 1, so
# ``resolve_process_group`` returned init's group (1) and the group-reclaim
# path executed ``os.killpg(1, SIGKILL)`` — which glibc turns into
# ``kill(-1, SIGKILL)``: every process owned by the user, including the
# daemon, VS Code and the SSH session, died. The symptom looked like an OOM /
# memory leak but was a signal. This fixture turns such a call into a loud
# test failure instead of a delivered signal.
_LETHAL_KILL_LOG_ENV = "TIANLUO_TEST_LETHAL_KILL_LOG"


def _lethal_signal_target(kind: str, target: int, own_pgrp: int) -> bool:
    """True when *target* would reach pytest itself or every user process."""
    try:
        target = int(target)
    except (TypeError, ValueError):
        return True  # a non-int (e.g. MagicMock) is never a valid target
    if kind == "killpg":
        # killpg(1) == kill(-1): the whole user; killpg(0) / own group: pytest.
        return target <= 1 or target == own_pgrp
    # kill(-1): the whole user; kill(0) / kill(-own): pytest's own group;
    # kill(1): init.
    return target in (-1, 0, 1) or target == -own_pgrp


@pytest.fixture(autouse=True)
def _forbid_lethal_process_signals(monkeypatch):
    """Refuse ``os.kill`` / ``os.killpg`` aimed at pgid<=1 or at pytest itself.

    Signal 0 (the aliveness probe) is refused too: it is the precursor of the
    lethal kill and is just as much a sign that a test fed a fake process into
    a real process-group path.
    """
    real_killpg = getattr(os, "killpg", None)
    real_kill = os.kill
    own_pgrp = os.getpgrp() if hasattr(os, "getpgrp") else -1
    log_path = os.environ.get(_LETHAL_KILL_LOG_ENV)

    def _refuse(kind: str, target, sig) -> None:
        nodeid = os.environ.get("PYTEST_CURRENT_TEST", "<unknown test>")
        msg = (
            f"refusing os.{kind}({target!r}, {sig!r}) from {nodeid}: it would "
            "signal pytest itself or every process of this user (this is what "
            "took the machine down on 2026-09-02/03). Give the fake process a "
            "real-looking pid or stub the process-group helpers."
        )
        if log_path:
            try:
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(msg + "\n")
            except OSError:
                pass
        raise RuntimeError(msg)

    def killpg(pgid, sig):
        if _lethal_signal_target("killpg", pgid, own_pgrp):
            _refuse("killpg", pgid, sig)
        return real_killpg(pgid, sig)

    def kill(pid, sig):
        if _lethal_signal_target("kill", pid, own_pgrp):
            _refuse("kill", pid, sig)
        return real_kill(pid, sig)

    if real_killpg is not None:
        monkeypatch.setattr(os, "killpg", killpg)
    monkeypatch.setattr(os, "kill", kill)
    yield
