"""Shared fixtures for the co-located engine tests (``src/se3/engine/test_*``).

The project's ``tests/`` suite is guarded by ``tests/conftest.py``, but pytest
only applies a ``conftest.py`` to its own directory subtree. The engine's
co-located test modules live under ``src/se3/engine/`` (a deliberate charter
exception for testing tightly-coupled engine internals), so they are OUTSIDE
``tests/`` and never saw that guard. This conftest restores the one guard those
tests need — see ``_no_real_code_index_refresh`` below.
"""

from pathlib import Path

import pytest


# Roots a co-located engine test must never write chat history into. The engine
# test module lives at ``src/se3/engine/`` so the repo root is four parents up;
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
def _no_chat_history_leak_to_real_repo(monkeypatch):
    """Neutralise chat-history writes aimed at the live repo for every engine test.

    Twin of the fixture in ``tests/conftest.py`` (kept in sync). ``pytest`` only
    applies a ``conftest.py`` to its own subtree, and the engine's co-located
    tests live OUTSIDE ``tests/``, so without this guard ``test_steps.py``'s
    ``test_test_success`` / ``test_test_failure`` — which run ``run_test_step``
    against a flow with no ``change_path``, so ``project_root`` falls back to
    ``Path.cwd()`` (the live repo) — leak a fake test-history jsonl pair into the
    committed ``se3/history/`` on every suite run.

    ``chat_history._append_message`` is the single disk-write point behind
    ``record_prompt`` / ``record_response``; wrapping it to no-op only when the
    resolved ``project_root`` is a real repo root drops those leaks while letting
    any tmp-scoped write through unchanged.
    """
    from se3.engine import chat_history

    real_roots = _real_history_roots()
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
def _no_real_code_index_refresh(monkeypatch):
    """Neutralise the flow-step code-index freshness hook for every engine test.

    Twin of the fixture in ``tests/conftest.py`` (kept in sync with it). Step
    handlers ``analyze`` (read-side) and ``commit`` (write-side) call
    ``context_builder.ensure_code_index_fresh(project_root)`` to lazily rebuild
    ``se3/code-index.md``. A test ``FlowInstance`` usually has no
    ``change_path``, so ``project_root`` falls back to ``Path.cwd()`` — the real
    se3 repo, which ships a committed ``se3/code-index.md``. The hook's
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
        "se3.engine.context_builder.ensure_code_index_fresh",
        lambda *args, **kwargs: None,
    )
