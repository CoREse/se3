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
