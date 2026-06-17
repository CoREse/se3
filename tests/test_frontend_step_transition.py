"""Pytest bridge for the web console's live step-transition / retry tests (G1).

The long-standing running-flow-console freeze reproduced two ways: when a flow
transitions discovery → analyze (the operator confirms the plan and the engine
steps into analyze), and when a later step (e.g. update_spec) fails and the
operator retries (the step re-runs reusing its step_id). In both cases the
daemon keeps pushing ``mode: append`` increments, and the incremental render
path MUST keep streaming them so the live view converges on the same
conversation a full ``mode: full`` reload would show — with no record lost, no
duplicate, and no cursor stall after an all-duplicate short-circuit.

The behavioural assertions live in the Node DOM-stub modules
``tests/frontend/live_append_step_transition.test.mjs`` and
``tests/frontend/live_append_retry_after_error.test.mjs``, both loaded and run
by ``tests/frontend/test_app_pure.mjs``. This pytest module pulls that suite
into the pytest run and asserts the G1 transition / retry checks actually
executed (rather than being silently skipped or dropped from the harness).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = REPO_ROOT / "tests" / "frontend"
HARNESS = FRONTEND_DIR / "test_app_pure.mjs"
TRANSITION_TEST = FRONTEND_DIR / "live_append_step_transition.test.mjs"
RETRY_TEST = FRONTEND_DIR / "live_append_retry_after_error.test.mjs"
E2E_TEST = FRONTEND_DIR / "live_append_e2e_consistency.test.mjs"
E2E_FIXTURE = FRONTEND_DIR / "fixtures" / "console_e2e_frames.json"


def test_transition_modules_present_and_registered():
    """The G1 mjs modules exist and are wired into the harness."""
    assert TRANSITION_TEST.is_file(), f"missing {TRANSITION_TEST}"
    assert RETRY_TEST.is_file(), f"missing {RETRY_TEST}"
    harness = HARNESS.read_text(encoding="utf-8")
    assert "registerLiveAppendStepTransitionTests" in harness, (
        "live_append_step_transition.test.mjs is not registered in test_app_pure.mjs"
    )
    assert "registerLiveAppendRetryAfterErrorTests" in harness, (
        "live_append_retry_after_error.test.mjs is not registered in test_app_pure.mjs"
    )


def test_e2e_consistency_module_present_and_registered():
    """The G4 end-to-end capstone module + its golden fixture exist and are wired."""
    assert E2E_TEST.is_file(), f"missing {E2E_TEST}"
    assert E2E_FIXTURE.is_file(), (
        f"missing golden fixture {E2E_FIXTURE} — regenerate with "
        "SE3_REGEN_GOLDEN=1 pytest tests/test_server_history_live_append_broadcast.py"
    )
    harness = HARNESS.read_text(encoding="utf-8")
    assert "registerConsoleE2EConsistencyTests" in harness, (
        "live_append_e2e_consistency.test.mjs is not registered in test_app_pure.mjs"
    )


def test_frontend_step_transition_node_suite_passes():
    """Run the Node assertion suite and confirm the G1 checks ran.

    Skipped if ``node`` is not available on PATH; the suite is still runnable by
    hand via ``node tests/frontend/test_app_pure.mjs``.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")
    assert HARNESS.is_file(), f"missing {HARNESS}"
    result = subprocess.run(
        [node, str(HARNESS)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"frontend test runner exited {result.returncode}:\n{combined}"
    )
    # The discovery→analyze transition and retry-after-error checks must have
    # actually executed — these are the regression-locking assertions.
    for needle in (
        "G1 transition: discovery→analyze keeps live-appending after the transition (no freeze)",
        "G1 transition: incremental append converges on the same result as a full reload",
        "G1 transition: a terminal step_completed is not deduped against a same-second step_output",
        "G1 transition: an all-duplicate append short-circuits but does NOT freeze the next genuine append",
        "G1 retry: recordKey distinguishes retrying vs running anchors at the same second",
        "G1 retry: live append keeps streaming the retry; region settles on the re-run running anchor",
        "G1 retry: incremental append converges on the same result as a full reload",
        # G4 end-to-end capstone — the golden-fixture daemon→server→frontend
        # bridge for both freeze scenarios.
        "G4 e2e (transition): live daemon→server→frontend stream converges with the full snapshot",
        "G4 e2e (retry): live daemon→server→frontend stream converges with the full snapshot",
        "G4 e2e (transition): post-confirmation analyze output renders live in the DOM",
        "G4 e2e (retry): post-failure retry output renders live in the DOM",
    ):
        assert needle in combined, (
            f"expected check {needle!r} in node output:\n{combined}"
        )
    assert "checks passed" in combined, combined
