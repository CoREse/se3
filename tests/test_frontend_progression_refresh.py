"""Pytest bridge for the web console's cause-immune progression-refresh fallback (G2).

The long-standing "flow advances (step switch / in-step retry) but the main
conversation freezes until you exit and re-enter the session" bug is worked
around — NOT root-caused — by watching the authoritative ``/api/flows/{id}``
snapshot (whose ``current_step`` / ``current_step_index`` advance on a step
switch, and whose ``status`` flips FAILED/PAUSED→RUNNING on an in-step retry —
note the daemon never emits ``step_history``, so it is deliberately not used)
and, on a detected advance of the open flow, firing
exactly one SILENT full ``/api/history`` rebuild (the G1 silent path: no blank
flash, scroll preserved, reply region untouched).

The DOM-stub behavioral assertions live in
``tests/frontend/progression_refresh.test.mjs``, which the Node assertion harness
``tests/frontend/test_app_pure.mjs`` loads and runs. This module pulls that suite
into the pytest run, asserts the G2 checks actually executed, and adds
static-source guardrails that the detector and its silent-refresh trigger are
wired into ``app.js``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "src" / "se3" / "server" / "static"
APP_JS = STATIC_DIR / "app.js"
FRONTEND_TEST = REPO_ROOT / "tests" / "frontend" / "test_app_pure.mjs"
PROGRESSION_TEST = REPO_ROOT / "tests" / "frontend" / "progression_refresh.test.mjs"


def test_progression_refresh_module_present():
    """The G2 mjs module exists and is registered into the harness."""
    assert PROGRESSION_TEST.is_file(), f"missing {PROGRESSION_TEST}"
    harness = FRONTEND_TEST.read_text(encoding="utf-8")
    assert "progression_refresh.test.mjs" in harness, (
        "progression_refresh.test.mjs is not registered in test_app_pure.mjs"
    )
    assert "registerProgressionRefreshTests" in harness


def test_frontend_progression_refresh_node_suite_passes():
    """Run the Node assertion suite and confirm the G2 checks ran.

    Skipped if ``node`` is not available on PATH; runnable by hand via
    ``node tests/frontend/test_app_pure.mjs``.
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
        "progression: first snapshot only sets baseline, no refresh",
        "progression: current_step change triggers exactly one refresh",
        "progression: only a retry/resume status flip triggers a refresh (not a halt)",
        "progression: only the open flow triggers a refresh",
        "progression: silent refresh keeps the DOM until new data arrives",
        "progression: silent refresh preserves scroll unless near bottom",
        "progression: silent refresh leaves reply-region state untouched",
        "progression: a stale out-of-order detail response does not regress the marker",
    ):
        assert needle in combined, (
            f"expected G2 check {needle!r} in node output:\n{combined}"
        )
    assert "checks passed" in combined, combined


def test_progression_detector_wired_into_refresh_flow_detail():
    """``refreshFlowDetail`` must run the progression detector against the
    authoritative ``data.flow`` snapshot, and the detector must fire the silent
    refresh through ``loadFlowConversation(..., { silent: true })``."""
    assert APP_JS.is_file(), f"missing {APP_JS}"
    js = APP_JS.read_text(encoding="utf-8")
    assert "function maybeRefreshConversationOnProgression(" in js, (
        "the progression detector helper must exist"
    )
    assert "maybeRefreshConversationOnProgression(data.flow)" in js, (
        "refreshFlowDetail must call the detector on the authoritative snapshot"
    )
    assert "flowProgressionMarker" in js, (
        "the progression baseline marker state must exist"
    )
    assert "{ silent: true }" in js, (
        "the detector must trigger the G1 silent full rebuild"
    )


def test_progression_detector_uses_status_not_dead_step_history():
    """The daemon's ``FlowSnapshot.to_dict()`` never emits ``step_history`` (the
    server back-fills it to an empty list), so a step_history-length signal is
    permanently dead and would miss the in-step-retry case (same ``step_id`` ⇒
    unchanged ``current_step``). The detector must instead key on the real
    ``status`` field, which flips FAILED/PAUSED→RUNNING when the operator retries
    a failed step."""
    js = APP_JS.read_text(encoding="utf-8")
    # The marker must carry the status discriminator and the advance test must
    # compare it, so an in-step retry (current_step / index unchanged) still
    # registers as progression.
    assert "status: marker.status" in js or "marker.status" in js, (
        "the progression detector must compare the flow status so an in-step "
        "retry (FAILED/PAUSED→RUNNING) is detected as progression"
    )
    # The dead step_history signal must NOT drive the detector any more.
    assert "stepHistoryLen" not in js, (
        "the detector must not rely on the always-empty step_history length"
    )


def test_silent_refresh_preserves_scroll_offset():
    """The silent refresh must capture and restore the reader's pre-rebuild
    ``scrollTop`` when they are not near the bottom, so the ``append=false``
    rebuild does not reset them to the top."""
    js = APP_JS.read_text(encoding="utf-8")
    assert "preserveScrollTop" in js, (
        "the silent refresh must capture the pre-rebuild scroll offset"
    )


def test_refresh_flow_detail_has_request_sequence_guard():
    """Concurrent detail fetches can resolve out of order; ``refreshFlowDetail``
    must drop a stale older response via a monotonic request-sequence guard so it
    cannot rewind the progression marker or re-fire the silent refresh."""
    js = APP_JS.read_text(encoding="utf-8")
    assert "flowDetailReqSeq" in js and "flowDetailAppliedSeq" in js, (
        "refreshFlowDetail must use a request-sequence freshness guard"
    )


def test_refresh_flow_detail_has_lifecycle_generation_guard():
    """The seq counters reset to 0 on openFlowView/doCloseFlowView, so a high-seq
    detail fetch left in flight from a prior open of the SAME flow would survive
    the selectedFlowId check and suppress the fresh post-reopen polls. A
    flow-view lifecycle generation must scope detail-response freshness across
    close/reopen so a prior-lifecycle response cannot apply or suppress newer
    snapshots."""
    js = APP_JS.read_text(encoding="utf-8")
    assert "flowDetailViewGen" in js, (
        "refreshFlowDetail must scope detail-response freshness to the "
        "flow-view lifecycle via flowDetailViewGen"
    )
