"""Pytest bridge for the web console's progression-refresh FAILURE SAFETY NET.

The "flow advances (step switch / in-step retry) but the main conversation
freezes until you exit and re-enter the session" bug (#209) is ROOT-CAUSE FIXED
by #243/#244 (the daemon push side now reads engine headers off the event loop,
so the WS history_data increment arrives on its own). The former "rebuild on
every advance" workaround is therefore DEMOTED to a failure safety net: on a
detected advance of the open flow (``current_step`` / ``current_step_index``
change, or a FAILED/PAUSED→RUNNING retry/resume — the daemon never emits
``step_history``, so it is deliberately not used) the detector arms a grace
window and fires the SILENT full ``/api/history`` rebuild ONLY IF no WS increment
landed for that flow (``flowConversationAppendSeq`` did not advance) before the
window elapsed. On the healthy path the fallback never fires.

The DOM-stub behavioral assertions live in
``tests/frontend/progression_refresh.test.mjs``, which the Node assertion harness
``tests/frontend/test_app_pure.mjs`` loads and runs. This module pulls that suite
into the pytest run, asserts the checks actually executed, and adds static-source
guardrails that the grace-timer state machine and its silent-refresh fallback are
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
    """Run the Node assertion suite and confirm the safety-net checks ran.

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
        "progression: first snapshot only sets baseline, no timer",
        "progression: advance + WS increment within grace → zero silent rebuilds",
        "progression: advance + no WS increment → periodic silent full rebuild that stays armed",
        "progression: a duplicate snapshot of the same advance re-fires nothing",
        "progression: only a retry/resume status flip arms a fallback (not a halt)",
        "progression: a halt-only status change (RUNNING→PAUSED) arms nothing",
        "progression: only the open flow arms a fallback",
        "progression: cancelling the grace drops a pending fallback",
        "progression: silent refresh keeps the DOM until new data arrives",
        "progression: silent refresh preserves scroll unless near bottom",
        "progression: silent refresh leaves reply-region state untouched",
        "progression: a stale out-of-order detail response does not regress the marker",
    ):
        assert needle in combined, (
            f"expected progression check {needle!r} in node output:\n{combined}"
        )
    assert "checks passed" in combined, combined


def test_progression_detector_wired_into_refresh_flow_detail():
    """``refreshFlowDetail`` must run the progression detector against the
    authoritative ``data.flow`` snapshot, and the detector's fallback must rebuild
    through ``loadFlowConversation(..., { silent: true })``."""
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
        "the fallback must rebuild through the silent full-reload path"
    )


def test_progression_fallback_is_a_grace_timed_safety_net():
    """The workaround is DEMOTED from "rebuild on every advance" to a failure
    safety net: on a detected advance the detector must arm a configurable grace
    timer and defer the silent rebuild, firing it only when the WS push path
    (tracked by ``flowConversationAppendSeq``) delivered no increment before the
    window elapsed. Pin the state-machine pieces so a regression to the old
    immediate-rebuild behavior fails loudly."""
    js = APP_JS.read_text(encoding="utf-8")
    # The WS-increment signal the fallback consults.
    assert "flowConversationAppendSeq" in js, (
        "the WS-increment monotonic counter must exist so the fallback can tell a "
        "silent WS path from a healthy one"
    )
    # The grace-timer state and its configurable window.
    assert "progressionGraceMs" in js, (
        "the grace window must be a configurable state field (default 5000ms)"
    )
    assert "progressionGraceMs: 5000" in js, (
        "the default grace window must be 5000ms"
    )
    assert "progressionGraceTimer" in js, (
        "the pending grace-timer handle must be tracked in state"
    )
    # The detector must schedule via setTimeout rather than rebuild inline.
    assert "state.progressionGraceMs" in js, (
        "the detector must schedule the fallback on the configurable grace window"
    )
    # A cancel helper must exist and be wired into the flow-view reset paths so a
    # pending fallback is dropped on flow switch/close.
    assert "function cancelProgressionGrace(" in js, (
        "a cancelProgressionGrace() helper must exist to drop a pending fallback"
    )
    assert js.count("cancelProgressionGrace()") >= 3, (
        "cancelProgressionGrace() must be called on a fresh advance re-arm and in "
        "both openFlowView and doCloseFlowView"
    )


def test_progression_append_seq_bumped_in_running_flow_branch():
    """``flowConversationAppendSeq`` is the authoritative "WS delivered an
    increment for the open flow" signal, so it must be bumped inside
    ``applyHistoryData``'s running-flow branch (the sole WS-append entry point),
    not from the history-view branch or a non-WS mutation."""
    js = APP_JS.read_text(encoding="utf-8")
    assert "state.flowConversationAppendSeq += 1" in js, (
        "applyHistoryData's running-flow branch must bump flowConversationAppendSeq "
        "when it actually lands new records"
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
