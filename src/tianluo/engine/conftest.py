"""Shared fixtures for the co-located engine tests (``src/tianluo/engine/test_*``).

The project's ``tests/`` suite is guarded by ``tests/conftest.py``, but pytest
only applies a ``conftest.py`` to its own directory subtree. The engine's
co-located test modules live under ``src/tianluo/engine/`` (a deliberate charter
exception for testing tightly-coupled engine internals), so they are OUTSIDE
``tests/`` and never saw that guard. This conftest restores the one guard those
tests need — see ``_no_real_code_index_refresh`` below.
"""

import os
from pathlib import Path

import pytest

# WHY: twin of the ``COLUMNS`` pop in ``tests/conftest.py`` (kept in sync); this
# subtree is outside ``tests/`` so that conftest never applies to it.
# pytest-xdist exports COLUMNS=80 into every worker process, and rich's
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


# Roots a co-located engine test must never write chat history into. The engine
# test module lives at ``src/tianluo/engine/`` so the repo root is four parents up;
# ``Path.cwd()`` is the fallback ``project_root`` for a flow with no
# ``change_path`` (as in ``test_steps.py``'s test-step cases), so include it too.
def _real_history_roots() -> set:
    roots = set()
    try:
        roots.add(Path(__file__).resolve().parents[3])
    except (OSError, IndexError):
        pass
    try:
        roots.add(Path.cwd().resolve())
    except OSError:
        pass
    return roots


@pytest.fixture(autouse=True)
def _no_chat_history_leak_to_real_repo(tmp_path, monkeypatch):
    """Neutralise chat-history writes aimed at the live repo for every engine test.

    Twin of the fixture in ``tests/conftest.py`` (kept in sync). ``pytest`` only
    applies a ``conftest.py`` to its own subtree, and the engine's co-located
    tests live OUTSIDE ``tests/``, so without this guard ``test_steps.py``'s
    ``test_test_success`` / ``test_test_failure`` — which run ``run_test_step``
    against a flow with no ``change_path``, so ``project_root`` falls back to
    ``Path.cwd()`` (the live repo) — leak a fake test-history jsonl pair into the
    committed ``tianluo/history/`` on every suite run.

    Every ``record_*`` writer resolves its target path through the module-global
    ``chat_history._history_dir`` (via ``_history_file``): ``record_prompt`` /
    ``record_response`` (through ``_append_message``) as well as the sibling
    writers ``record_step_event`` / ``record_stream_progress`` /
    ``record_user_interjection`` and every other ``record_*``. Rerouting that one
    resolution point into a per-test tmp dir when the resolved ``project_root``
    is a real repo root drops the leak from ALL writers at once while letting any
    tmp-scoped write through unchanged. ``tianluo.engine.state_machine`` binds
    ``_history_dir`` at import time, so its reference is repatched directly too.
    """
    from tianluo.engine import chat_history

    real_roots = _real_history_roots()
    redirect_root = tmp_path / "_chat_history_redirect"
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
def _no_real_code_index_refresh(monkeypatch):
    """Neutralise the flow-step code-index freshness hook for every engine test.

    Twin of the fixture in ``tests/conftest.py`` (kept in sync with it). Step
    handlers ``analyze`` (read-side) and ``commit`` (write-side) call
    ``context_builder.ensure_code_index_fresh(project_root)`` to lazily rebuild
    ``tianluo/code-index.md``. A test ``FlowInstance`` usually has no
    ``change_path``, so ``project_root`` falls back to ``Path.cwd()`` — the real
    luo repo, which ships a committed ``tianluo/code-index.md``. The hook's
    "no map yet → skip" guard then no longer fires, so it runs a *real*
    incremental build against the live repo: it takes an exclusive ``flock``
    (concurrent test processes deadlock on it) and spawns a real LLM summariser
    subprocess for every stale node. When the committed map has drifted from the
    working tree that becomes a multi-minute-per-test rebuild — which is exactly
    what timed the suite out here (``test_steps.py::TestAnalyzeStep`` alone spent
    ~50s+ per test rebuilding the index).

    Unit tests must never trigger a real code-index build, so the hook is a
    no-op by default. The dedicated code_index tests exercise the builder
    directly via ``build_index`` / ``load_or_build`` (not this hook) and are
    unaffected; a test that specifically wants the real hook can re-patch it.
    """
    monkeypatch.setattr(
        "tianluo.engine.context_builder.ensure_code_index_fresh",
        lambda *args, **kwargs: None,
    )


@pytest.fixture(autouse=True)
def _force_en_us_ui_language(monkeypatch):
    """Pin the i18n UI language to en-US for co-located engine tests.

    Twin of the fixture in ``tests/conftest.py`` (kept in sync). ``tianluo.i18n``
    resolves the UI language lazily from ``Path.cwd()`` — the repo root, whose
    ``tianluo.yaml`` sets ``language: zh-CN`` — so a ``t()``-rendered display string
    (e.g. ``render_usage_block``) would render Chinese and break an English
    assertion. ``SE3_LANG=en-US`` (highest precedence) plus a singleton reset
    keeps rendered text the stable en-US reference for these tests too.
    """
    import tianluo.i18n as _i18n

    monkeypatch.setenv("SE3_LANG", "en-US")
    _i18n.reset_language()
    _i18n.clear_caches()
    yield
    _i18n.reset_language()
    _i18n.clear_caches()


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
