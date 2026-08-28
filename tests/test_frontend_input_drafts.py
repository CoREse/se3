"""Pytest bridge for the web console's local input-draft cache (G1).

The four prompt boxes in the WebUI — the docked reply textarea shared by
respond/interject (`#flow-reply-input`), the New Task description (`#nt-task`)
and the New Issue description/title (`#issue-description` / `#issue-title`) —
keep whatever was typed and not sent. A draft is a per-device fact ("the words
this browser has not sent yet"), so it lives in `localStorage` only and is never
shipped to the server.

The behavioural assertions live in the Node DOM-stub suite
`tests/frontend/input_drafts.test.mjs`, which the assertion harness
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
DRAFTS_TEST = REPO_ROOT / "tests" / "frontend" / "input_drafts.test.mjs"
APP_JS = REPO_ROOT / "src" / "tianluo" / "server" / "static" / "app.js"


def test_input_drafts_module_present():
    """The registrable mjs module exists and is wired into the harness."""
    assert DRAFTS_TEST.is_file(), f"missing {DRAFTS_TEST}"
    harness = FRONTEND_TEST.read_text(encoding="utf-8")
    assert "input_drafts.test.mjs" in harness, (
        "input_drafts.test.mjs is not registered in test_app_pure.mjs"
    )
    assert "registerInputDraftTests" in harness


def test_every_localstorage_touch_is_guarded():
    """Static guard: the draft store never reaches localStorage unguarded.

    ``localStorage`` throws on mere property access in some privacy modes and
    throws ``QuotaExceededError`` when full. The draft cache is a convenience,
    so a storage failure must degrade to "there is no draft" — never to an input
    box that will not take text. The single accessor ``draftStorage()`` is the
    only place the global is touched, and it is wrapped; every read/write helper
    goes through it and wraps its own JSON/`setItem` call.
    """
    src = APP_JS.read_text(encoding="utf-8")
    # The one place the global is named for real (plus the pre-existing
    # i18n / ws-debug uses, which carry their own try/catch).
    assert "function draftStorage()" in src
    start = src.index("function draftStorage()")
    body = src[start : src.index("\n}", start)]
    assert "try {" in body and "catch" in body, (
        "draftStorage() must guard the localStorage access itself"
    )
    for fn in ("readDraftEntries", "writeDraftEntries"):
        head = src.index(f"function {fn}(")
        chunk = src[head : src.index("\n}\n", head)]
        assert "catch" in chunk, f"{fn} must not let a storage fault escape"


def test_frontend_input_drafts_node_suite_passes():
    """Run the Node assertion suite and confirm the draft checks ran.

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
        timeout=120,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"frontend test runner exited {result.returncode}:\n{combined}"
    )
    for needle in (
        # Bounded storage: entry cap, TTL, and the pure pruning helper.
        "G1 pruneDraftEntries drops expired entries and caps at the newest N",
        "G1 the store is bounded: MAX_ENTRIES survives, the oldest fall out",
        "G1 the draft being typed right now is never the one evicted",
        "G1 an entry older than the TTL reads as no draft",
        # Text only — the attachment strip is never persisted.
        "G1 only text is persisted — the stored entry carries nothing else",
        # Isolation by input position, including per-flow reply drafts.
        "G1 draftKeyForInput gives each of the four boxes its own slot",
        "G1 reply drafts are per-flow — one flow's words never surface in another",
        # Debounced write and the refill, with auto-grow re-measured.
        "G1 typing saves behind a debounce: one write, the latest text",
        "G1 restoring a multi-line draft re-measures the auto-grow textarea",
        "G1 opening a flow (resetReplyBox) refills that flow's draft and re-grows",
        # EVERY existing clear path — missing one leaves a stale draft behind.
        "G1 clear path 1/4 — a delivered reply drops its flow draft",
        "G1 clear path 2/4 — a delivered interject drops the same slot",
        "G1 clear path 3/4 — a structured approve/reject drops the flow draft",
        "G1 clear path 4/4 — a published task drops the new-task draft",
        "G1 a created issue drops both issue drafts; an edit leaves them alone",
        # ...and only the form it was submitted from: a modal reopened during
        # the round trip belongs to different work and must be left alone.
        "G1 a create that lands after the modal moved on leaves the new form alone",
        "G1 a FAILED send keeps the draft — nothing was delivered",
        # Storage failures degrade to "no draft", never to a dead input.
        "G1 storage that throws on every access still leaves the boxes usable",
        "G1 no localStorage at all behaves the same way",
        "G1 a QuotaExceededError on write is swallowed, and reads still work",
        "G1 a send still succeeds when storage is dead",
    ):
        assert needle in combined, (
            f"expected input-draft check {needle!r} in node output:\n{combined}"
        )
    assert "checks passed" in combined, combined
