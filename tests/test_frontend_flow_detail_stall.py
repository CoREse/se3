"""Pytest bridge for the flow view's connection-pool starvation fix (G3).

Symptom: opening a LARGE completed flow left the left sidebar on its
"Loading flow details…" placeholder forever — stuck, never an error. Two
frontend facts produced it:

* ``pollFlowView`` fired ``refreshFlowDetail()`` + ``selfHealFlowConversation()``
  every 3s with no in-flight guard (``flowConversationEpoch`` supersedes a
  RESPONSE, it never skips a REQUEST), and while the held record set is still
  empty those "silent" polls request the BARE full bundle — tens of MB and
  seconds of server work each. Past ~6 x 3s of per-response wall time the
  browser's per-origin connection pool was full of bundle pulls and
  ``/api/flows/{id}`` was queued behind them indefinitely.
* ``authedFetch`` was a bare ``fetch`` with no deadline and no AbortController,
  so a request the browser never put on the wire yielded neither ``!resp.ok``
  nor a ``catch``: ``noteDetailFetchFailure`` never ran and the placeholder was
  never replaced by the "Retrying…" copy.

The behavioural assertions live in ``tests/frontend/flow_detail_stall.test.mjs``,
which the Node harness ``tests/frontend/test_app_pure.mjs`` loads and runs; this
module pulls that suite into the pytest run, asserts the checks actually
executed, and adds the static guardrails a DOM-stub test cannot see: the i18n
keys behind the new copy, and that the 12.14.0 lazy-detail delivery semantics
were NOT rolled back to buy the fix.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC = REPO_ROOT / "src" / "tianluo" / "server" / "static"
APP_JS = STATIC / "app.js"
I18N = STATIC / "i18n"
SERVER = REPO_ROOT / "src" / "tianluo" / "server"
FRONTEND_TEST = REPO_ROOT / "tests" / "frontend" / "test_app_pure.mjs"
STALL_TEST = REPO_ROOT / "tests" / "frontend" / "flow_detail_stall.test.mjs"

NEW_I18N_KEYS = ("flow.detailTimeout", "flow.conversationTimeout")

MARKERS = ("(S1)", "(S3)", "(S4)", "(S5)", "(S6)", "(S8)", "(S9)")


def _app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _function_body(src: str, signature: str) -> str:
    start = src.index(signature)
    return src[start:src.index("\n}\n", start)]


def test_stall_module_is_registered():
    assert STALL_TEST.is_file(), f"missing {STALL_TEST}"
    harness = FRONTEND_TEST.read_text(encoding="utf-8")
    assert "flow_detail_stall.test.mjs" in harness
    assert "registerFlowDetailStallTests" in harness


def test_frontend_stall_suite_runs():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    proc = subprocess.run(
        [node, str(FRONTEND_TEST)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for marker in MARKERS:
        assert f"ok - {marker}" in proc.stdout, (marker, proc.stdout[-4000:])


# --------------------------------------------------------------------------
# i18n
# --------------------------------------------------------------------------


def test_new_states_are_rendered_through_i18n():
    src = _app_js()
    for key in NEW_I18N_KEYS:
        assert f'tf("{key}"' in src, f"{key} must be rendered via tf()"


def test_en_us_holds_the_new_keys_and_every_locale_is_a_subset():
    """en-US is the baseline holding the full key set (charter rule)."""
    baseline = json.loads((I18N / "en-US.json").read_text(encoding="utf-8"))
    for key in NEW_I18N_KEYS:
        assert key in baseline, f"en-US must hold {key}"
        assert baseline[key].strip(), f"{key} must not be empty"
    for path in sorted(I18N.glob("*.json")):
        if path.name == "en-US.json":
            continue
        other = json.loads(path.read_text(encoding="utf-8"))
        extra = set(other) - set(baseline)
        assert not extra, f"{path.name} holds keys en-US does not: {sorted(extra)}"


def test_zh_cn_translates_the_new_keys():
    zh = json.loads((I18N / "zh-CN.json").read_text(encoding="utf-8"))
    en = json.loads((I18N / "en-US.json").read_text(encoding="utf-8"))
    for key in NEW_I18N_KEYS:
        assert key in zh, f"zh-CN must translate {key}"
        assert zh[key] != en[key], f"{key} must actually be translated"


# --------------------------------------------------------------------------
# static guardrails on the fix itself
# --------------------------------------------------------------------------


def test_authed_fetch_carries_a_deadline_and_an_abort_controller():
    """A request with no ceiling can wedge a connection slot for the page's life."""
    body = _function_body(_app_js(), "async function authedFetch(")
    assert "AbortController" in body, (
        "authedFetch must be able to hang up on a request, or a queued request "
        "keeps occupying one of the browser's ~6 connection slots forever"
    )
    assert "FETCH_TIMEOUTS" in body and "setTimeout(" in body, (
        "authedFetch must impose a wall-clock deadline — a request that neither "
        "resolves nor rejects is what left the sidebar 'stuck' rather than 'error'"
    )


def test_the_history_bundle_deadline_outlives_the_server_pull_budget():
    """A healthy cold pull must not be cancelled on principle."""
    src = _app_js()
    start = src.index("const FETCH_TIMEOUTS = {")
    block = src[start:src.index("};", start)]
    assert "historyBundle" in block
    bundle_ms = int(block.split("historyBundle:")[1].split(",")[0].strip())
    pull_timeout_s = float(
        (SERVER / "app.py").read_text(encoding="utf-8")
        .split("HISTORY_PULL_TIMEOUT = ")[1].split("\n")[0].strip()
    )
    assert bundle_ms > pull_timeout_s * 1000, (
        "the browser must give the server longer than the server's own daemon "
        f"pull budget ({pull_timeout_s}s), got {bundle_ms}ms"
    )


def test_the_poll_cannot_stack_bundle_pulls():
    src = _app_js()
    body = src[src.index("async function loadFlowConversation("):]
    body = body[:body.index("\nfunction scrollFlowConversationToBottom(")]
    assert "state.flowConversationInFlight" in body, (
        "the silent 3s self-heal must skip while a pull is already outstanding"
    )
    assert "flowConversationDeferredSelfHeal" in body, (
        "a skipped tick must be remembered and made up, not silently dropped"
    )


def test_a_deliberate_cancel_is_not_counted_as_a_detail_failure():
    body = _function_body(_app_js(), "async function refreshFlowDetail(")
    assert "isAbortError(err)" in body, (
        "a flow switch aborts the outgoing detail read; counting that as a "
        "failure would paint 'Retrying…' over a healthy sidebar on every switch"
    )
    assert "flow.detailTimeout" in body, (
        "a deadline must reach noteDetailFetchFailure with its own copy"
    )


def test_switching_or_closing_the_flow_view_releases_its_requests():
    src = _app_js()
    for fn in ("function openFlowView(", "function doCloseFlowView("):
        body = _function_body(src, fn)
        assert "abortFlowViewFetches()" in body, f"{fn} must release the old view's requests"


# --------------------------------------------------------------------------
# the 12.14.0 lazy-detail semantics must survive this fix
# --------------------------------------------------------------------------


def test_lazy_detail_delivery_is_not_rolled_back():
    """Wire still carries collapsed summaries; bodies still come on demand."""
    src = _app_js()
    assert "/detail?tool_use_id=" in src or ("tool_use_id=" in src and "/detail" in src)
    app_py = (SERVER / "app.py").read_text(encoding="utf-8")
    assert '@app.get("/api/history/{flow_id}/detail")' in app_py
    body = _function_body(app_py.replace("\n\n\n", "\n}\n\n"), "async def _history_response(")
    assert "summarize_history_records(" in body, (
        "every history delivery still leaves through the summarizing funnel"
    )


def test_the_on_demand_detail_read_is_cancellable_with_the_view():
    """A body fetched for a flow the user left must not hold a slot."""
    src = _app_js()
    start = src.index("const resp = await authedFetch(lazyDetailUrl(ref)")
    assert 'scope: "flow"' in src[start:start + 200], (
        "the lazy tool-detail read belongs to the flow view's cancellation set"
    )
