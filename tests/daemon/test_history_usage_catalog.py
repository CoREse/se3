"""Daemon-side lock: the pricing catalog rides usage-bearing history frames.

The server re-aggregates its cached records whenever a usage-bearing append
lands, but it cannot reach the project's ``tianluo.yaml`` (that lives on the
owning machine). The daemon therefore serializes the project's effective
pricing catalog onto ANY frame whose records carry usage — full or append —
so the server's rebuild prices the same records with the same table the
daemon's full-frame payload used. A catalog-less rebuild would flip priced
estimates to unknown-price and degrade completeness to partial between
frames.
"""

from __future__ import annotations

import json

import pytest

from tianluo.daemon.history import DaemonHistoryReader
from tianluo.daemon.protocol import HISTORY_MODE_APPEND, HISTORY_MODE_FULL
from tianluo.usage import UsageRecord, UsageStatus

FLOW = "20260813-120000_deadbeef"
STEP_FILE = "01_implement_ab12.jsonl"

# Override prices distinct from the built-in opus-5 rates so the assertions
# below prove the PROJECT catalog — not the built-in table — priced the frame.
PRICING_YAML = """
pricing:
  models:
    claude-opus-5:
      input: 1.0
      output: 2.0
      cache_read: 0.5
      cache_creation: 0.5
      cache_creation_5m: 0.5
      cache_creation_1h: 0.5
"""


def _record(call_id, tokens):
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
        output_tokens=100,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        actual_cost_usd=None,
    )


def _usage_msg(call_id, tokens):
    return {
        "role": "assistant",
        "content": "implemented",
        "usage_records": [_record(call_id, tokens).to_dict()],
    }


def _write_jsonl(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )


def _append_jsonl(path, lines):
    with path.open("a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")


def _hist(root):
    return root / "tianluo" / "history" / FLOW


def test_full_read_carries_usage_priced_with_project_catalog(tmp_path):
    """A complete full snapshot carries the usage payload AND the serialized
    project catalog; the payload's estimate reflects the project override."""
    (tmp_path / "tianluo.yaml").write_text(PRICING_YAML, encoding="utf-8")
    _write_jsonl(_hist(tmp_path) / STEP_FILE, [_usage_msg("c1", 1000)])
    reader = DaemonHistoryReader(project_roots_provider=lambda: [str(tmp_path)])

    read = reader.read_flow(FLOW, project_root=str(tmp_path))
    assert read.mode == HISTORY_MODE_FULL
    assert read.usage is not None
    assert read.usage_catalog is not None
    assert read.usage_catalog["version"]
    assert "claude-opus-5" in read.usage_catalog["entries"]
    # 1000 uncached input + 100 output at the override 1.0 / 2.0 USD/M.
    assert read.usage["summary"]["estimated_cost_usd"] == pytest.approx(
        1e-3 + 2e-4
    )
    assert read.usage["summary"]["unknown_price_count"] == 0


def test_append_read_carries_catalog_but_no_payload(tmp_path):
    """An append delta carries the catalog (so the server re-aggregates with
    the project's prices) but no usage payload — a delta under-counts, so the
    payload still rides full snapshots only."""
    (tmp_path / "tianluo.yaml").write_text(PRICING_YAML, encoding="utf-8")
    step = _hist(tmp_path) / STEP_FILE
    _write_jsonl(step, [_usage_msg("c1", 1000)])
    reader = DaemonHistoryReader(project_roots_provider=lambda: [str(tmp_path)])

    first = reader.read_flow(FLOW, project_root=str(tmp_path))
    _append_jsonl(step, [_usage_msg("c2", 2000)])
    read = reader.read_flow(
        FLOW, project_root=str(tmp_path), cursor=first.cursor
    )
    assert read.mode == HISTORY_MODE_APPEND
    assert read.usage is None
    assert read.usage_catalog is not None
    assert "claude-opus-5" in read.usage_catalog["entries"]


def test_append_without_usage_omits_catalog(tmp_path):
    """A frame carrying no usage needs no catalog on the wire (the server
    leaves its stored payload untouched), so the key stays off."""
    (tmp_path / "tianluo.yaml").write_text(PRICING_YAML, encoding="utf-8")
    step = _hist(tmp_path) / STEP_FILE
    _write_jsonl(step, [{"role": "assistant", "content": "no usage here"}])
    reader = DaemonHistoryReader(project_roots_provider=lambda: [str(tmp_path)])

    first = reader.read_flow(FLOW, project_root=str(tmp_path))
    _append_jsonl(step, [{"role": "assistant", "content": "still none"}])
    read = reader.read_flow(
        FLOW, project_root=str(tmp_path), cursor=first.cursor
    )
    assert read.mode == HISTORY_MODE_APPEND
    assert read.usage is None
    assert read.usage_catalog is None


def test_unknown_root_degrades_catalog_to_builtin(tmp_path):
    """No tianluo.yaml (or an unresolvable root): the frame still carries a
    catalog — the built-in table — so the server never rebuilds catalog-less."""
    _write_jsonl(_hist(tmp_path) / STEP_FILE, [_usage_msg("c1", 1000)])
    reader = DaemonHistoryReader(project_roots_provider=lambda: [str(tmp_path)])

    read = reader.read_flow(FLOW, project_root=str(tmp_path))
    assert read.usage is not None
    assert read.usage_catalog is not None
    assert "claude-opus-5" in read.usage_catalog["entries"]
    # Built-in opus-5 uncached-input price (5.0 USD/M) — not the override.
    assert read.usage["summary"]["estimated_cost_usd"] != 1e-3 + 2e-4
