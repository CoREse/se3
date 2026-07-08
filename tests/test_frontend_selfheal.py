"""Pytest bridge for the web console's ordinal-identity idempotent reconcile (G2).

The right-side running-flow console no longer treats the WS `mode:append`
increment as the source of truth. Each record carries a stable `stepId#ordinal`
identity (the daemon injects the 0-based line position — see daemon/history.py),
and the reconcile is idempotent: an increment can arrive any number of times and
always converges to the same result. A retry that rewrites a line updates it in
place; a dropped/mis-judged increment self-heals at the next periodic full
snapshot. This removes the "chat stops advancing" freeze as a class.

The behavioural assertions live in the Node assertion harness
``tests/frontend/test_app_pure.mjs``, which loads
``tests/frontend/marker_dedup_ordinal.test.mjs`` and
``tests/frontend/incremental_selfheal.test.mjs``. This module pulls that suite
into the pytest run and asserts the G2 checks actually executed (not silently
skipped).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = REPO_ROOT / "tests" / "frontend"
FRONTEND_TEST = FRONTEND_DIR / "test_app_pure.mjs"
MARKER_TEST = FRONTEND_DIR / "marker_dedup_ordinal.test.mjs"
SELFHEAL_TEST = FRONTEND_DIR / "incremental_selfheal.test.mjs"


def test_g2_reconcile_modules_present():
    """The registrable G2 mjs modules exist and are wired into the harness."""
    assert MARKER_TEST.is_file(), f"missing {MARKER_TEST}"
    assert SELFHEAL_TEST.is_file(), f"missing {SELFHEAL_TEST}"
    harness = FRONTEND_TEST.read_text(encoding="utf-8")
    assert "marker_dedup_ordinal.test.mjs" in harness, (
        "marker_dedup_ordinal.test.mjs is not registered in test_app_pure.mjs"
    )
    assert "registerMarkerDedupOrdinalTests" in harness
    assert "incremental_selfheal.test.mjs" in harness, (
        "incremental_selfheal.test.mjs is not registered in test_app_pure.mjs"
    )
    assert "registerIncrementalSelfHealTests" in harness


def test_frontend_g2_reconcile_node_suite_passes():
    """Run the Node assertion suite and confirm the G2 reconcile checks ran.

    Skipped if ``node`` is not available on PATH; the suite is still runnable by
    hand via ``node tests/frontend/test_app_pure.mjs``.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")
    assert FRONTEND_TEST.is_file(), f"missing {FRONTEND_TEST}"
    result = subprocess.run(
        [node, str(FRONTEND_TEST)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"frontend test runner exited {result.returncode}:\n{combined}"
    )
    for needle in (
        # ordinal identity + marker distinctness
        "G2 recordKey uses stepId#ordinal when the envelope carries an ordinal",
        "G2 empty-content marker records stay distinct by ordinal (no false collision)",
        "G2 a record without an ordinal falls back to the legacy content key",
        # idempotent reconcile core
        "G2 reconcile: a byte-identical re-delivery of a line is a no-op (changed=false)",
        "G2 reconcile: a retry rewrite of the same ordinal updates in place (not dropped/duped)",
        "G2 reconcile: same ordinal arriving many times converges to one record",
        # the discovery-freeze scenario end to end
        "G2 applyHistoryData: a PAUSE→resume rewrite of a discovery line advances the view live",
        # the commit index_progress card renders in place then shows the result
        "G2 commit: the index_progress card updates in place across the whole rebuild, then the commit result content shows",
        # self-heal via the periodic full snapshot
        "G2 self-heal: a dropped append frame is recovered by the next full snapshot",
        "G2 self-heal: a full snapshot deletes a stale bubble no longer in the history",
        "G2 self-heal: re-delivering the SAME full snapshot renders no duplicate bubbles (render idempotent)",
    ):
        assert needle in combined, (
            f"expected G2 check {needle!r} in node output:\n{combined}"
        )
    assert "checks passed" in combined, combined
