"""Pytest bridge for the web console's message history + arrow-key recall (G2).

The two prompt boxes that carry a conversation — the docked reply textarea
shared by respond/interject (`#flow-reply-input`) and the New Task description
(`#nt-task`) — recall text the operator has actually **sent**. A sent message
belongs to the *owner*, not to the browser it was typed in, so it is persisted
server-side (see `tests/server/test_message_history.py`) and the console only
mirrors it. The navigation semantics deliberately copy the CLI's
(`prompt_toolkit` in multiline mode): ↑ reaches history only with the caret on
the first line, ↓ only on the last.

The behavioural assertions live in the Node DOM-stub suite
`tests/frontend/message_history.test.mjs`, which the assertion harness
`tests/frontend/test_app_pure.mjs` loads and runs. This pytest module pulls that
suite into the pytest run, asserts the new checks actually executed (not
silently skipped), and statically guards the two wiring facts a behavioural
check cannot see from inside the stub: that history is bound as an ADDITIONAL
listener beside the existing Ctrl/Cmd+Enter submit and auto-grow bindings, and
that the history endpoint is reached with plain ``fetch`` rather than
``authedFetch`` (a 401 on background bookkeeping must not throw the whole
console back to the login gate). Skipped when ``node`` is not available on PATH;
the suite is still runnable by hand via ``node tests/frontend/test_app_pure.mjs``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_TEST = REPO_ROOT / "tests" / "frontend" / "test_app_pure.mjs"
HISTORY_TEST = REPO_ROOT / "tests" / "frontend" / "message_history.test.mjs"
APP_JS = REPO_ROOT / "src" / "tianluo" / "server" / "static" / "app.js"


def test_message_history_module_present():
    """The registrable mjs module exists and is wired into the harness."""
    assert HISTORY_TEST.is_file(), f"missing {HISTORY_TEST}"
    harness = FRONTEND_TEST.read_text(encoding="utf-8")
    assert "message_history.test.mjs" in harness, (
        "message_history.test.mjs is not registered in test_app_pure.mjs"
    )
    assert "registerMessageHistoryTests" in harness


def test_history_binding_does_not_displace_the_existing_reply_bindings():
    """Static guard: the reply textarea keeps its submit and auto-grow bindings.

    History navigation is registered as its own ``keydown`` listener precisely
    so the Ctrl/Cmd+Enter submit chord and the ``input``→auto-grow binding stay
    exactly as they were; replacing either of them would be a silent regression
    that the DOM stub cannot notice on its own.
    """
    src = APP_JS.read_text(encoding="utf-8")
    assert 'bindMessageHistory("flow-reply-input");' in src
    assert 'bindMessageHistory("nt-task");' in src
    # The pre-existing bindings on the same element are still installed.
    assert '(e.ctrlKey || e.metaKey) && e.key === "Enter"' in src
    assert '$("flow-reply-input").addEventListener("input", autoGrowReplyTextarea);' in src


def test_history_fetch_is_unauthed_and_never_blocks_the_input():
    """Static guard: history uses plain fetch and swallows its own failures.

    ``authedFetch`` drives the console back to the login gate on a 401. History
    is best-effort background bookkeeping layered on top of the prompt, so it
    must not be able to trigger that transition — and every one of its network
    touches has to be wrapped, since an unreachable server may never block
    typing or submitting.
    """
    src = APP_JS.read_text(encoding="utf-8")
    for fn in ("ensureMessageHistoryLoaded", "postMessageHistory"):
        head = src.index(f"function {fn}(")
        chunk = src[head : src.index("\n}\n", head)]
        assert "authedFetch" not in chunk, (
            f"{fn} must not use authedFetch — a background 401 would log the user out"
        )
        assert "catch" in chunk, f"{fn} must not let a network fault escape"


def test_frontend_message_history_node_suite_passes():
    """Run the Node assertion suite and confirm the history checks ran.

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
        timeout=180,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"frontend test runner exited {result.returncode}:\n{combined}"
    )
    for needle in (
        # CLI-parity caret semantics and the bounded stack.
        "G2 caretAtFirstLine / caretAtLastLine mark the two edges of a multi-line box",
        "G2 orderHistoryPush: blanks dropped, an immediate repeat dropped, cap holds",
        "G2 ↑ recalls only from the first line; mid-text it stays caret movement",
        "G2 ↓ walks forward only from the last line, and restores the stashed edit",
        # Channels, content-dependent UI, and the bindings that must survive.
        "G2 the two channels never cross",
        "G2 a recalled entry re-measures the auto-grow textarea",
        "G2 Ctrl/Cmd+Enter still submits and a modified arrow is left to the browser",
        # Lazy load and every degradation path.
        "G2 history is lazy: nothing is fetched until the box is touched",
        "G2 an unreachable history endpoint degrades to this session's own list",
        "G2 a 401 from the history endpoint is not an auth transition",
        "G2 the remote list merges under this session's sends without duplicating",
        "G2 signing out drops the cached history",
        # Every delivery path pushes; the failure path does not.
        "G2 push path 1/4 — a delivered reply enters the reply channel",
        "G2 push path 2/4 — a delivered interject shares that channel",
        "G2 push path 3/4 — a rejection note enters history, a bare approval does not",
        "G2 push path 4/4 — a published task enters the new-task channel",
        "G2 a FAILED send records nothing — undelivered text is not history",
        "G2 a send resets the cursor so the next ↑ starts at the newest entry",
        # Identity, and the cursor that has to follow it: an append in flight is
        # judged by the server, a fold moves a live traversal onto the surviving
        # row, and the cap evicting the displayed entry skips nothing.
        "G2 a send awaiting its id stays recallable, and folds only once the POST fails",
        "G2 a delayed fold keeps the traversal on the row it folded onto",
        "G2 an entry evicted by the cap under a live traversal skips nothing",
        # 先定序、后截断: a pending append occupies no permanent slot, and an
        # append the cap has finished with is never resurrected as the newest.
        "G2 a pending append that folds gives back the entry the cap displaced",
        "G2 an append displaced by later sends is never resurrected as newest",
        "G2 opening a flow / the New Task modal leaves no stale cursor behind",
    ):
        assert needle in combined, (
            f"expected message-history check {needle!r} in node output:\n{combined}"
        )
    assert "checks passed" in combined, combined
