"""Pytest bridge for the WebUI's lazy tool-call details.

``GET /api/history/{flow_id}`` now delivers a session in COLLAPSED-STATE form:
a successful tool call's body is stripped server-side and fetched on expand via
``GET /api/history/{flow_id}/detail``. The behavioural assertions live in
``tests/frontend/history_lazy_detail.test.mjs``, which the Node harness
``tests/frontend/test_app_pure.mjs`` loads and runs; this module pulls that
suite into the pytest run, asserts the checks actually executed, and adds the
static guardrails a pure-DOM test cannot see: the i18n keys behind the new
states, their CSS, the on-demand endpoint the frontend calls, and — the ones
that decide whether this is safe to ship — that the realtime WebSocket push and
the daemon→server upstream protocol were left alone.
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
STYLE_CSS = STATIC / "style.css"
I18N = STATIC / "i18n"
SERVER = REPO_ROOT / "src" / "tianluo" / "server"
FRONTEND_TEST = REPO_ROOT / "tests" / "frontend" / "test_app_pure.mjs"
LAZY_TEST = REPO_ROOT / "tests" / "frontend" / "history_lazy_detail.test.mjs"

NEW_I18N_KEYS = (
    "tool.detail.loading",
    "tool.detail.unavailable",
    # "View raw" restores what the summary held back, and says so when it
    # cannot — both states are user-visible text.
    "raw.loading",
    "raw.unavailable",
)


def test_lazy_detail_module_is_registered():
    assert LAZY_TEST.is_file(), f"missing {LAZY_TEST}"
    harness = FRONTEND_TEST.read_text(encoding="utf-8")
    assert "history_lazy_detail.test.mjs" in harness
    assert "registerHistoryLazyDetailTests" in harness


def test_frontend_lazy_detail_suite_runs():
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
    # Every lazy-detail check must have actually executed, not merely compiled.
    for marker in ("(L1)", "(L2)", "(L3/L4)", "(L5)", "(L6)", "(L7)", "(L8)",
                   "(L9)", "(L10)", "(L11)", "(L12)", "(L13)", "(L14)",
                   "(L15)", "(L16)", "(L17)", "(L18)"):
        assert f"ok - {marker}" in proc.stdout, (marker, proc.stdout[-4000:])


# --------------------------------------------------------------------------
# i18n
# --------------------------------------------------------------------------


def test_new_states_are_rendered_through_i18n():
    src = APP_JS.read_text(encoding="utf-8")
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


def test_transient_detail_states_are_styled():
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".tool-detail-loading" in css
    assert ".tool-detail-unavailable" in css


# --------------------------------------------------------------------------
# static guardrails on the seam
# --------------------------------------------------------------------------


def test_frontend_calls_the_on_demand_endpoint():
    src = APP_JS.read_text(encoding="utf-8")
    assert "/detail?tool_use_id=" in src.replace('"\n    + "', "") or (
        "tool_use_id=" in src and "/detail" in src
    )
    app_py = (SERVER / "app.py").read_text(encoding="utf-8")
    assert '@app.get("/api/history/{flow_id}/detail")' in app_py


def test_upstream_daemon_protocol_is_untouched():
    """The daemon→server leg must not have grown a message type for this."""
    proto = (REPO_ROOT / "src" / "tianluo" / "daemon" / "protocol.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("tool_detail_request", "MSG_TOOL_DETAIL", "history_detail_request"):
        assert forbidden not in proto, (
            "the summary/detail split is a server→browser change only; the "
            f"upstream protocol must not learn {forbidden!r}"
        )
    # The relay reuses the existing history pull rather than a new frame.
    app_py = (SERVER / "app.py").read_text(encoding="utf-8")
    start = app_py.index('@app.get("/api/history/{flow_id}/detail")')
    # To the next route, so the assertion covers the whole handler however it
    # grows rather than a fixed byte window.
    body = app_py[start:app_py.index("@app.get(", start + 10)]
    assert "_pull_history_from_daemon(" in body


def test_the_relay_shapes_by_origin_not_by_transport():
    """Requirement: a REPLAY is summarized on WebSocket too; a live one is not.

    The behavioural halves live in
    ``tests/server/test_history_lazy_detail.py``; this pins the structural rule
    they rest on — the ``/ws/ui`` relay decides from the frame's ORIGIN (does it
    answer a pull this server dispatched?) rather than from its ``mode``, which
    a multi-frame recovery makes indistinguishable from the live push loop.
    """
    ws_py = (SERVER / "ws.py").read_text(encoding="utf-8")
    start = ws_py.index("async def _push_history_data(")
    body = ws_py[start:ws_py.index("\n\n\n", start)]
    assert "summarize_history_records(records, flow_id) if replay else records" in body, (
        "a replay frame is shaped exactly like the REST bundle response, and a "
        "live tail append rides whole — one verdict per frame"
    )
    # The verdict is per FRAME and browser-independent, so the relay hands the
    # hub ONE payload for the whole owner...
    assert "broadcast_owned(frame, owner)" in body
    # ...and it may not reach for any of the inputs that made the same frame
    # leave the server in several shapes.
    ws_all = ws_py + (SERVER / "state.py").read_text(encoding="utf-8")
    for forbidden in (
        "record_created_at", "note_history_clock", "subscribers_of",
        "subscribed_at", "keep_whole",
    ):
        assert forbidden not in ws_all, (
            "the summarize/whole verdict must not consult record timestamps, "
            f"clock offsets or per-browser subscription instants ({forbidden})"
        )
    # The origin marker is armed on the one funnel every回程 pull leaves
    # through, so no pull path can dispatch without it.
    start = ws_py.index("async def request_history(")
    body = ws_py[start:ws_py.index("\n\n\n", start)]
    assert "mark_history_replay(" in body


def test_summary_shaping_sits_on_the_single_response_funnel():
    app_py = (SERVER / "app.py").read_text(encoding="utf-8")
    start = app_py.index("async def _history_response(")
    body = app_py[start:app_py.index("\n\n\n", start)]
    assert "summarize_history_records(" in body, (
        "every history delivery (full / delta / backfill / reconciled) leaves "
        "through _history_response, so the shaping belongs there"
    )


def test_both_views_share_the_lazy_render_path():
    """Requirement: the running-flow console and the history pane, together.

    They are the same renderer — the history pane's entry point delegates to
    `renderConversation` — so the lazy chip path cannot land in one and miss
    the other.
    """
    src = APP_JS.read_text(encoding="utf-8")
    start = src.index("function renderHistoryRecords(")
    body = src[start:src.index("\n}", start)]
    assert "renderConversation(" in body
    # Both loaders read the same summarized bundle endpoint, through the SAME
    # pair of URL builders — the windowed open and the token-echoing poll. Named
    # rather than pattern-matched on the literal path so the two views cannot
    # drift onto different request shapes (only one of which would be windowed).
    for fn in ("async function loadFlowConversation(", "async function openHistorySession("):
        if fn in src:
            seg = src[src.index(fn):][:12000]
            assert "historyWindowUrl(" in seg, fn
            assert "historySnapshotUrl(" in seg, fn


def test_view_raw_restores_what_the_summary_held_back():
    """View raw exists to show the record as recorded, not as summarized."""
    src = APP_JS.read_text(encoding="utf-8")
    for fn in ("function makeRawToggle(", "function makeAssistantRawToggle(",
               "function makeUserRawToggle("):
        start = src.index(fn)
        body = src[start:src.index("\n}\n", start)]
        assert "paintRawInto(" in body, (
            f"{fn} must fetch the held-back bodies instead of printing stubs"
        )


def test_header_verbatim_inputs_are_exempt_from_elision():
    """A path is read WHOLE / tail-first by the chip headers, so it stays whole."""
    summary = (SERVER / "history_summary.py").read_text(encoding="utf-8")
    assert "VERBATIM_INPUT_KEYS" in summary
    for key in ('"file_path"', '"path"'):
        assert key in summary, key


def test_failure_details_are_never_lazified():
    """INVARIANT: an auto-expanded failure chip must not need a round trip."""
    summary = (SERVER / "history_summary.py").read_text(encoding="utf-8")
    assert "is_error is not True" in summary
    assert "_failed_tool_use_ids" in summary
