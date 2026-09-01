"""Pytest bridge for the WebUI's interrupted-delivery recovery loop.

A big completed session's history is delivered as ~150 daemon frames, and a
socket that dies mid-drain leaves the server's bundle a self-consistent PREFIX:
its cursor names exactly the step files that landed and its pending window is
empty, so the frontend's numbered ``stepId#ordinal`` self-check finds nothing to
repair. The server DOES know (``ServerState._OpenDelivery`` →
``incomplete: true``), but until this change that statement reached nobody — the
WebSocket frames did not carry it, the frontend read it from neither face, and
the History view has no poll timer of its own, so the conversation's
commit/summarize tail stayed invisible.

The behavioural assertions live in the Node DOM-stub suite
``tests/frontend/history_incomplete_recovery.test.mjs``, which the harness
``tests/frontend/test_app_pure.mjs`` loads and runs. This module pulls that suite
into the pytest run, asserts the checks actually executed, and statically guards
the two wire-level facts the loop depends on: both server-side push frames carry
``incomplete``, and the frontend reads it from the REST reply as well. Skipped
when ``node`` is not on PATH; the suite is still runnable by hand via
``node tests/frontend/test_app_pure.mjs``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_TEST = REPO_ROOT / "tests" / "frontend" / "test_app_pure.mjs"
RECOVERY_TEST = (
    REPO_ROOT / "tests" / "frontend" / "history_incomplete_recovery.test.mjs"
)
APP_JS = REPO_ROOT / "src" / "tianluo" / "server" / "static" / "app.js"
WS_PY = REPO_ROOT / "src" / "tianluo" / "server" / "ws.py"


def test_recovery_module_is_wired_into_the_harness():
    assert RECOVERY_TEST.is_file(), f"missing {RECOVERY_TEST}"
    harness = FRONTEND_TEST.read_text(encoding="utf-8")
    assert "history_incomplete_recovery.test.mjs" in harness
    assert "registerHistoryIncompleteRecoveryTests" in harness


def test_both_push_frames_carry_the_completeness_statement():
    """``incomplete`` must ride BOTH WS faces, not just the REST snapshot.

    A console watching a flow over the push path alone (the History view, which
    has no poll timer) learns that what it is showing is a prefix only from the
    frame it receives — from the ``history_data`` frame when the records are
    relayed, and from the records-less ``history_cursor`` advisory when they are
    suppressed. A statement carried by only one of the two leaves whichever path
    the flow actually takes uninformed.
    """
    src = WS_PY.read_text(encoding="utf-8")
    data_frame = src[src.index("async def _push_history_data") : src.index(
        "async def _push_history_cursor("
    )]
    assert 'frame["incomplete"] = meta["incomplete"]' in data_frame
    advisory = src[src.index("async def _push_history_cursor(") : src.index(
        "async def _push_history_cursor_advisory("
    )]
    assert '"incomplete": meta["incomplete"]' in advisory


def test_the_cold_advisory_declares_the_delivery_incomplete():
    """The stand-in for a DROPPED frame must state completeness too.

    ``_push_history_cursor_advisory`` replaces a frame whose records the cache
    threw away (the flow was budget-evicted), which is the strongest evidence a
    delivery is unfinished — never that it is whole. It has no bundle to read the
    flag off, so it states it outright; omitting it would leave the console's
    only signal for that frame silent about the one fact it must not guess.
    """
    src = WS_PY.read_text(encoding="utf-8")
    advisory = src[src.index("async def _push_history_cursor_advisory("):]
    advisory = advisory[: advisory.index("async def _push_spawn_failed(")]
    assert '"incomplete": True' in advisory


def test_an_absent_statement_is_not_read_as_settled():
    """Silence must leave the last statement — and the repair — untouched.

    A pushed frame carries no ``incomplete`` key when the cache holds no bundle
    to read it from, and an older server never carries one. ``!incomplete`` would
    read that absence as an explicit "settled", cancelling an armed recovery and
    latching the bundle whole — which, for the History view (no poll timer of its
    own), retires the only repair path there is.
    """
    src = APP_JS.read_text(encoding="utf-8")
    body = src[src.index("function noteBundleCompleteness("):]
    body = body[: body.index("function scheduleIncompleteRecovery(")]
    guard = "if (incomplete === undefined || incomplete === null) return;"
    assert guard in body
    # …and it precedes BOTH the signal count and the settled branch: a frame that
    # says nothing is not an answer an attempt may count, nor a declaration.
    assert body.index(guard) < body.index("state.incompleteRecoverySignals[key] =")
    assert body.index(guard) < body.index("if (!incomplete) {")


def test_rest_replies_are_read_for_completeness_on_both_views():
    """Both REST loaders act on the flag, before any render branch.

    An interrupted bundle is answered ``not_modified`` forever, which is a
    "nothing to repaint" branch — so a completeness read placed after the render
    decision would never run for exactly the bundles that need it.
    """
    src = APP_JS.read_text(encoding="utf-8")
    assert 'merged.incomplete = !!(response && response.incomplete);' in src
    for view in ("flow", "history"):
        assert f'noteBundleCompleteness("{view}", flowId, result.incomplete);' in src


def test_frontend_incomplete_recovery_node_suite_passes():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")
    assert FRONTEND_TEST.is_file(), f"missing {FRONTEND_TEST}"
    result = subprocess.run(
        [node, str(FRONTEND_TEST)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=180,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"frontend test runner exited {result.returncode}:\n{combined}"
    )
    for needle in (
        "(I1/I2) an `incomplete` bundle is re-read until the server says it is settled",
        "(I3) a LIVE flow's own 3s poll owns the repair",
        "(I6) a re-read that fails transiently is retried, never abandoned",
        "(I7) the streak is bounded",
        "(I8) a frame carrying NO completeness statement never settles a bundle",
        "(I8b) silence with nothing said yet stays `not said yet`",
        "(I4) closing a view cancels the re-read it armed",
        "(I5) mergeHistoryResponse surfaces `incomplete` on every delivery shape",
    ):
        assert needle in combined, (
            f"expected recovery check {needle!r} in node output:\n{combined}"
        )
    assert "checks passed" in combined, combined
