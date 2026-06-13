"""Pytest bridge for the web console's reply-send error-handling (issue #193).

The running-flow console's `sendReply` / `appendLocalReply` pair had a
regression: clicking "确认并继续 (输入 1)" against a discovery_confirm pending
item POSTed the "1" (which the backend received and acted on), but a fault in
the post-success conversation rendering fell into `sendReply`'s too-wide
network-error catch — so a delivered confirm was reported as
"Could not send — network error reaching the server" and its optimistic echo
never appeared in the message list. This is the same class of "backend
succeeded, frontend falsely reported failure" bug as the #191 webui-issue-create
false timeout.

The behavioural assertions live in the Node DOM-stub suite
`tests/frontend/reply_send_error_handling.test.mjs`, which the assertion harness
`tests/frontend/test_app_pure.mjs` loads and runs. This pytest module pulls that
suite into the pytest run, asserts the new checks actually executed (not
silently skipped), and statically guards that the module is wired into the
harness. Skipped when ``node`` is not available on PATH; the suite is still
runnable by hand via ``node tests/frontend/test_app_pure.mjs``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_TEST = REPO_ROOT / "tests" / "frontend" / "test_app_pure.mjs"
REPLY_SEND_TEST = REPO_ROOT / "tests" / "frontend" / "reply_send_error_handling.test.mjs"


def test_reply_send_module_present():
    """The registrable mjs module exists and is wired into the harness."""
    assert REPLY_SEND_TEST.is_file(), f"missing {REPLY_SEND_TEST}"
    harness = FRONTEND_TEST.read_text(encoding="utf-8")
    assert "reply_send_error_handling.test.mjs" in harness, (
        "reply_send_error_handling.test.mjs is not registered in test_app_pure.mjs"
    )
    assert "registerReplySendErrorHandlingTests" in harness


def test_frontend_reply_send_node_suite_passes():
    """Run the Node assertion suite and confirm the reply-send checks ran.

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
    # The reply-send error-handling checks must have actually executed, not
    # silently been skipped.
    for needle in (
        "G1 appendLocalReply baseline: a normal reply lands as a tagged user echo",
        "G1 appendLocalReply records the echo before a post-success render throws and never bubbles",
        "G1 appendLocalReply ignores a reply for a non-selected flow",
        # The integrated sendReply checks observe the actual user-facing #193
        # symptom (success-branch toast vs. network-error catch), not just the
        # appendLocalReply helper in isolation.
        "G2 sendReply: delivered '1' whose post-success render throws still shows success, not a network error",
        "G2 sendReply: a clean successful send shows the success toast and the echo",
        "G2 sendReply: a genuine fetch network failure DOES surface the network-error toast",
    ):
        assert needle in combined, (
            f"expected reply-send check {needle!r} in node output:\n{combined}"
        )
    assert "checks passed" in combined, combined
