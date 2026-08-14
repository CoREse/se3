"""Daemon usage-summary source parity with the CLI history path.

``flow_usage_summary`` must read the same record sources the CLI's
``_state_usage_payload`` uses — session ledger, else the union of
``step.outputs.usage_records``, else the legacy five-field tally — so the
WebUI and ``luo history show`` never disagree on the same flow.
"""

from __future__ import annotations

import json

from tianluo.daemon.usage_backend import flow_usage_summary
from tianluo.usage import UsageRecord, UsageStatus


def _record(call_id, tokens=100):
    return UsageRecord(
        call_id=call_id,
        attempt=0,
        usage_status=UsageStatus.AVAILABLE,
        agent_name="claude",
        runner_type="claude-code",
        provider="anthropic",
        resolved_model="claude-opus-5",
        logical_input_tokens=tokens,
        uncached_input_tokens=tokens,
        output_tokens=10,
    ).to_dict()


def test_session_ledger_wins():
    state = {"session_usage_records": [_record("c1")]}
    summary = flow_usage_summary(state, call_id="flow")
    assert summary is not None
    assert summary["totals"]["logical_input_tokens"] == 100


def test_inline_step_outputs_recover_when_ledger_empty(tmp_path):
    state = {
        "steps": {
            "01_implement": {
                "outputs": {"usage_records": [_record("c2", 200)]},
            },
        },
    }
    summary = flow_usage_summary(state, project_root=tmp_path, call_id="flow")
    assert summary is not None
    assert summary["totals"]["logical_input_tokens"] == 200


def test_cold_ref_step_outputs_recover_when_ledger_empty(tmp_path):
    flow_id = "20260813-120000_deadbeef"
    cold_dir = tmp_path / "tianluo" / "state" / "steps" / flow_id
    cold_dir.mkdir(parents=True)
    (cold_dir / "01_implement.json").write_text(
        json.dumps({"outputs": {"usage_records": [_record("c3", 300)]}}),
        encoding="utf-8",
    )
    state = {
        "steps": {
            "01_implement": {
                "step_id": "01_implement",
                "cold_ref": {"file": "01_implement.json", "hash": "x"},
            },
        },
    }
    summary = flow_usage_summary(
        state, project_root=tmp_path, call_id=flow_id, flow_id=flow_id
    )
    assert summary is not None
    assert summary["totals"]["logical_input_tokens"] == 300


def test_legacy_tally_is_last_resort(tmp_path):
    state = {"session_token_usage": {"input_tokens": 40, "output_tokens": 4}}
    summary = flow_usage_summary(state, project_root=tmp_path, call_id="flow")
    assert summary is not None
    assert summary["totals"]["logical_input_tokens"] == 40
    # The legacy tally's uncached input is split out (40 input, no cache),
    # matching the CLI's adapted ledger instead of a zero-uncached record.
    assert summary["totals"]["uncached_input_tokens"] == 40


def test_unavailable_records_kept_in_daemon_and_counted():
    record = _record("c4")
    unavailable = UsageRecord(
        call_id="c5", attempt=0, usage_status=UsageStatus.UNAVAILABLE
    ).to_dict()
    state = {"session_usage_records": [record, unavailable]}
    summary = flow_usage_summary(state, call_id="flow")
    assert summary is not None
    assert summary["unknown_call_count"] == 1
    assert summary["completeness"] == "partial"


def test_modern_empty_ledger_reports_no_usage(tmp_path):
    """A modern flow before its first LLM call must not fabricate a call.

    ``session_usage_records: []`` (present, empty) means "zero calls so far";
    only a pre-ledger state — one WITHOUT that key — may adapt the legacy
    five-field tally into a legacy_ambiguous record.
    """
    state = {
        "session_usage_records": [],
        "session_token_usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "total_cost_usd": 0.0,
        },
    }
    assert flow_usage_summary(state, project_root=tmp_path, call_id="flow") is None


def test_round_tripped_legacy_tally_still_adapts(tmp_path):
    """An empty ledger beside a NON-zero tally is legacy data, not zero calls.

    The modern serializer writes ``session_usage_records: []`` for a re-saved
    pre-ledger flow, so keying on the key's presence alone would drop real
    accumulated usage the moment such a flow is persisted again.
    """
    state = {
        "session_usage_records": [],
        "session_token_usage": {
            "input_tokens": 5000,
            "output_tokens": 400,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 200,
            "total_cost_usd": 1.23,
        },
    }
    summary = flow_usage_summary(state, project_root=tmp_path, call_id="flow")
    assert summary is not None
    assert summary["totals"]["logical_input_tokens"] == 5300
    assert summary["totals"]["uncached_input_tokens"] == 5000
    assert summary["actual_cost_usd"] == 1.23


def test_step_records_still_win_over_round_tripped_legacy_tally(tmp_path):
    """Recoverable per-call records outrank the legacy projection."""
    state = {
        "session_usage_records": [],
        "session_token_usage": {"input_tokens": 5000, "total_cost_usd": 1.23},
        "steps": {
            "01_implement": {"outputs": {"usage_records": [_record("c7", 700)]}},
        },
    }
    summary = flow_usage_summary(state, project_root=tmp_path, call_id="flow")
    assert summary is not None
    assert summary["totals"]["logical_input_tokens"] == 700


def test_archived_cold_step_outputs_recover_when_ledger_empty(tmp_path):
    """``clear_state`` moves cold step files under ``state/archive/steps/``.

    The CLI history path is archive-aware, so the daemon must resolve there too
    or the two surfaces disagree for the same archived flow.
    """
    flow_id = "20260813-130000_cafebabe"
    cold_dir = tmp_path / "tianluo" / "state" / "archive" / "steps" / flow_id
    cold_dir.mkdir(parents=True)
    (cold_dir / "01_implement.json").write_text(
        json.dumps({"outputs": {"usage_records": [_record("c6", 321)]}}),
        encoding="utf-8",
    )
    state = {
        "session_usage_records": [],
        "steps": {
            "01_implement": {
                "step_id": "01_implement",
                "cold_ref": {"file": "01_implement.json", "hash": "x"},
            },
        },
    }
    summary = flow_usage_summary(
        state, project_root=tmp_path, call_id=flow_id, flow_id=flow_id
    )
    assert summary is not None
    assert summary["totals"]["logical_input_tokens"] == 321
