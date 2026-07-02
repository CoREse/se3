"""Shared fixtures for the co-located engine tests (``src/se3/engine/test_*``).

The project's ``tests/`` suite is guarded by ``tests/conftest.py``, but pytest
only applies a ``conftest.py`` to its own directory subtree. The engine's
co-located test modules live under ``src/se3/engine/`` (a deliberate charter
exception for testing tightly-coupled engine internals), so they are OUTSIDE
``tests/`` and never saw that guard. This conftest restores the one guard those
tests need — see ``_no_real_code_index_refresh`` below.
"""

import pytest


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
