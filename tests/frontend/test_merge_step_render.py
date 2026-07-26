"""Guardrails for the retirement of the webui ``merging`` bypass status (G8).

The worktree merge used to surface as a *bypass* sub-state layered on a COMPLETED
flow: a ``merging`` boolean flowed engine.json → daemon aggregator/history →
server → frontend, where ``isMerging`` / ``flowStatusLabel`` folded it into a
"合并中" badge and ``chat_history.record_merging`` streamed a "合并中" chat anchor.
G5 moved the merge into the flow's own ``merge_integrate`` + ``version_reconcile``
steps (executed in the main checkout under the merge lock), and G8 removes the
now-redundant bypass entirely: merge progress is shown by those two steps'
ordinary step rendering, not a special-cased sub-state.

Two contracts are pinned here:

1. **No bypass residue.** The ``merging`` flag, its ``record_merging`` anchor,
   the ``isMerging`` helper, the ``merging`` STEP_STATUS_DISPLAY / badge / chat
   anchor, and the engine.json schema field are all gone — nothing NEW writes a
   ``merging`` sub-state. (The frontend still *reads* a legacy ``{"type":"merging"}``
   row from pre-change archived flows tolerantly, folding it into a benign
   step-event status row instead of a stray empty bubble — see
   ``test_normalize_record_recognizes_legacy_merging_event``.)
2. **Merge renders as steps.** ``merge_integrate`` / ``version_reconcile`` are
   real step types that a worktree flow appends to its sequence, and the frontend
   gives them first-class step titles so they render like every other step.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = REPO_ROOT / "src" / "se3" / "server" / "static"
APP_JS = STATIC_DIR / "app.js"
STYLE_CSS = STATIC_DIR / "style.css"
SRC = REPO_ROOT / "src" / "se3"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing {path}"
    return path.read_text(encoding="utf-8")


def _extract_js_function_body(src: str, name: str) -> str:
    """Return the brace-balanced body of ``function <name>(...) { ... }``."""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
    assert m, f"could not locate function {name!r} in app.js"
    open_idx = src.index("{", m.end())
    depth = 0
    for i in range(open_idx, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[open_idx : i + 1]
    raise AssertionError(f"unbalanced braces while scanning function {name!r}")


def _extract_const_block(src: str, name: str) -> str:
    """Return the literal text of a ``const <name> = { … };`` block."""
    m = re.search(
        r"const\s+" + re.escape(name) + r"\s*=\s*\{.*?^\};\s*$",
        src,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert m, f"could not locate const {name!r} block in app.js"
    return m.group(0)


# ---------------------------------------------------------------------------
# 1. The merging bypass is fully retired
# ---------------------------------------------------------------------------


def test_app_js_has_no_is_merging_helper():
    """The ``isMerging`` bypass predicate must be gone — the merge is no longer a
    completed-body sub-state, so there is nothing to detect."""
    src = _read(APP_JS)
    assert "function isMerging(" not in src, (
        "isMerging must be removed — merge progress renders via the merge steps"
    )
    # And it must not be exported / referenced anywhere either.
    assert "isMerging(" not in src, "no call site of isMerging may remain"


def test_flow_status_label_has_no_merging_override():
    """``flowStatusLabel`` must no longer override the status with 合并中 — it only
    folds in the (running) waiting-for-lock sub-state now."""
    body = _extract_js_function_body(_read(APP_JS), "flowStatusLabel")
    assert "合并中" not in body, (
        "flowStatusLabel must not emit a 合并中 override — the merge is a step now"
    )
    assert "isMerging" not in body


def test_step_status_display_has_no_merging_entry():
    """The chat status-anchor display map must not carry a ``merging`` status —
    no ``merging`` lifecycle anchor is ever written now."""
    block = _extract_const_block(_read(APP_JS), "STEP_STATUS_DISPLAY")
    assert "merging" not in block, (
        "STEP_STATUS_DISPLAY must not define a 'merging' status entry"
    )
    # waiting_for_lock is a *different*, still-live sub-state and must remain.
    assert "waiting_for_lock" in block, (
        "the waiting_for_lock anchor must be preserved"
    )


def test_normalize_record_recognizes_legacy_merging_event():
    """No NEW ``merging`` anchor is ever written, but pre-change archived worktree
    flows (real old flows in se3/history) still carry a bare ``{"type":"merging"}``
    row. ``normalizeRecord`` (the daemon→webui raw-record path) must recognize it
    as a lifecycle anchor — folded into the same step-event family as
    ``waiting_for_lock`` — so it does NOT fall through to the generic role path and
    render as a stray empty "(no readable content)" bubble (the CLI reader
    chat_history.get_step_history already skips it symmetrically)."""
    body = _extract_js_function_body(_read(APP_JS), "normalizeRecord")
    assert '=== "merging"' in body, (
        "normalizeRecord must recognize a legacy 'merging' event type so old "
        "archived flows don't render a stray empty bubble"
    )
    # It must be handled in the SAME lifecycle-anchor branch as waiting_for_lock,
    # i.e. it returns a role 'step-event' record, not a generic bubble.
    assert '=== "waiting_for_lock"' in body


def test_normalize_record_maps_legacy_merging_to_step_event():
    """The legacy ``merging`` record maps to a role ``step-event`` anchor (kind
    ``merging``), so renderConversationRecord routes it to renderStepStartedRecord
    (a benign status row) rather than the generic empty-bubble path."""
    render_body = _extract_js_function_body(
        _read(APP_JS), "renderConversationRecord"
    )
    assert 'norm.kind === "merging"' in render_body, (
        "renderConversationRecord must route a legacy 'merging' anchor to the "
        "status-row renderer, not the generic bubble path"
    )


def test_style_css_has_no_merging_rules():
    """The 合并中 badge / chat-status CSS must be removed."""
    css = _read(STYLE_CSS)
    assert ".badge-merging" not in css, ".badge-merging rule must be removed"
    assert "step-status-merging" not in css, (
        ".conv-record.step-status-merging rule must be removed"
    )


def test_models_flow_instance_has_no_merging_field():
    """``FlowInstance`` must not carry a ``merging`` field, and its serialized
    dict must not emit the key — the bypass flag is gone."""
    from tianluo.engine.models import FlowInstance

    flow = FlowInstance(flow_id="x")
    assert not hasattr(flow, "merging"), "FlowInstance.merging must be removed"
    assert "merging" not in flow.to_dict()
    # A legacy engine.json still carrying the key must round-trip without error
    # and without resurrecting the attribute (backward-compatible ignore).
    revived = FlowInstance.from_dict({**flow.to_dict(), "merging": True})
    assert not hasattr(revived, "merging")


def test_chat_history_has_no_record_merging():
    """``chat_history`` must no longer expose ``record_merging`` — nothing writes
    the bypass anchor anymore."""
    import tianluo.engine.chat_history as chat_history

    assert not hasattr(chat_history, "record_merging"), (
        "record_merging must be removed"
    )


def test_engine_json_schema_has_no_merging_property():
    """The engine.json schema must not declare a ``merging`` property."""
    from tianluo.engine.schema import ENGINE_JSON_SCHEMA

    props = ENGINE_JSON_SCHEMA.get("properties", {})
    assert "merging" not in props, "engine.json schema must drop the merging key"
    # waiting_for_lock (a live sub-state) is still declared.
    assert "waiting_for_lock" in props


@pytest.mark.parametrize(
    "module_path",
    [
        SRC / "daemon" / "aggregator.py",
        SRC / "daemon" / "history.py",
        SRC / "server" / "state.py",
    ],
)
def test_propagation_layers_drop_merging_flag(module_path: Path):
    """The daemon→server propagation snapshots must not carry a ``merging``
    attribute assignment (``merging=`` / ``"merging":``) anymore — the flag has
    no source and no consumer."""
    src = _read(module_path)
    assert 'merging=' not in src, f"{module_path.name} must not set a merging field"
    assert '"merging"' not in src, (
        f"{module_path.name} must not serialize a merging key"
    )


# ---------------------------------------------------------------------------
# 2. Merge progress renders through the two merge steps
# ---------------------------------------------------------------------------


def test_merge_steps_have_frontend_step_titles():
    """``merge_integrate`` / ``version_reconcile`` must have first-class titles in
    both the step-header map and the report-card map, so they render as named
    steps (the replacement for the retired 合并中 bypass) rather than degrading to
    a raw step_type literal."""
    src = _read(APP_JS)
    header = _extract_const_block(src, "STEP_HEADER_TITLES")
    report = _extract_const_block(src, "STEP_REPORT_TITLES")
    for key in ("merge_integrate", "version_reconcile"):
        assert key in header, f"STEP_HEADER_TITLES must title {key!r}"
        assert key in report, f"STEP_REPORT_TITLES must title {key!r}"


def test_merge_step_types_exist_in_engine():
    """The two merge step types must exist with the exact string values the
    frontend title maps key off."""
    from tianluo.engine.models import STEP_POOL, StepType

    assert StepType.MERGE_INTEGRATE.value == "merge_integrate"
    assert StepType.VERSION_RECONCILE.value == "version_reconcile"
    # Registered in the step pool so the engine can instantiate/route them.
    assert StepType.MERGE_INTEGRATE in STEP_POOL
    assert StepType.VERSION_RECONCILE in STEP_POOL


def test_worktree_flow_appends_merge_steps(tmp_path):
    """A worktree flow's sequence ends with integrate → reconcile, so the merge
    is part of the flow's normal, renderable step sequence — the mechanism that
    replaces the bypass status. Idempotent (no duplication on a re-derive)."""
    from tianluo.engine.models import StepType
    from tianluo.engine.state_machine import StateMachine

    sm = StateMachine(tmp_path)
    base = [StepType.IMPLEMENT, StepType.COMMIT]
    appended = sm._append_worktree_merge_steps(base)
    assert appended[-2:] == [StepType.MERGE_INTEGRATE, StepType.VERSION_RECONCILE]
    # Idempotent: re-appending does not duplicate.
    assert sm._append_worktree_merge_steps(appended) == appended
