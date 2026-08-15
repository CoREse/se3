"""``read_flow`` × ``record_budget`` — the delivery-side half of the fix.

``record_budget`` is unit-tested on its own; what these tests lock down is its
*wiring* into the reader, which is where the invariants that matter to a user
live:

* an oversized record leaves the reader within the raw_json budget, with all of
  its tool chips still present and in order — the WebUI reconciles chips by
  ``step_id#ordinal``, so a chip lost here never comes back on a later frame;
* ``ordinal`` is unchanged by compaction, because it is a physical line number
  and the frontend's idempotent reconcile keys on it;
* both read branches (incremental seek-read and full re-read) go through the
  SAME compaction entry point, so a record does not change shape depending on
  which branch happened to deliver it;
* the ~99 % of records below the fast-path gate are not touched, not traversed,
  and not even serialised;
* the chunk budget bills the COMPACTED size, so a record that folds down to a
  few KB no longer costs a whole round-trip of its own.
"""

from __future__ import annotations

import json
import logging

import pytest

from tianluo.daemon import history as history_mod
from tianluo.daemon import record_budget as rb
from tianluo.daemon.history import MAX_BYTES_PER_REPORT, DaemonHistoryReader


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


CHIP_COUNT = 206


def _make_reader(*roots):
    return DaemonHistoryReader(project_roots_provider=lambda: [str(r) for r in roots])


def _flow_dir(root, flow_id):
    d = root / "tianluo" / "history" / flow_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _thinking_event(index):
    return {
        "type": "system",
        "subtype": "thinking_tokens",
        "estimated_tokens": 50 * index,
        "estimated_tokens_delta": 50,
        "uuid": "uuid-think-%d" % index,
        "session_id": "sess-1",
    }


def _tool_use_event(index, body):
    return {
        "type": "assistant",
        "message": {
            "id": "msg-%d" % index,
            "role": "assistant",
            "model": "claude-opus-5",
            "content": [
                {"type": "tool_use", "id": "toolu_%04d" % index,
                 "name": "Bash", "input": {"command": body}},
            ],
        },
    }


def _tool_result_event(index, body):
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"tool_use_id": "toolu_%04d" % index, "type": "tool_result",
                 "is_error": False, "content": body},
            ],
        },
    }


def _result_event():
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "total_cost_usd": 1.8261305,
        "usage": {"input_tokens": 49, "output_tokens": 14317},
    }


def _oversized_record(chips=CHIP_COUNT, body_bytes=80 * 1024, thinking_per_chip=8):
    """A record shaped like the reported 16 MB+ ``discovery`` record.

    ``206`` ``tool_result`` events buried in telemetry, with the terminal
    ``result`` event dead last — the position a budget-exhaustion truncation
    would eat first, which is exactly why compaction must not work that way.
    """
    events = [{"type": "system", "subtype": "init", "cwd": "/tmp"}]
    counter = 0
    for chip in range(chips):
        for _ in range(thinking_per_chip):
            counter += 1
            events.append(_thinking_event(counter))
        body = ("payload-%05d " % chip) * (body_bytes // 15)
        events.append(_tool_use_event(chip, "run step %d" % chip))
        events.append(_tool_result_event(chip, body))
    events.append(_result_event())
    return {
        "role": "assistant",
        "content": "final answer",
        "raw_json": events,
        "timestamp": "2026-08-15T19:40:18",
        "step_type": "discovery",
        "token_usage": {"input_tokens": 49, "output_tokens": 14317},
        "usage_records": [{"model": "claude-opus-5", "output_tokens": 14317}],
    }


def _telemetry_heavy_record(telemetry=40000):
    """Oversized purely through zero-render telemetry.

    Folding alone brings this one far below a delivery chunk, which is what
    makes it the record that distinguishes raw-size billing from compacted-size
    billing.
    """
    events = [_thinking_event(i) for i in range(telemetry)]
    events.insert(len(events) // 2, _tool_use_event(0, "ls"))
    events.insert(len(events) // 2 + 1, _tool_result_event(0, "ok"))
    events.append(_result_event())
    return {
        "role": "assistant",
        "content": "short",
        "raw_json": events,
        "step_type": "implement",
    }


def _small_record(index):
    return {
        "role": "assistant",
        "content": "small record %d %s" % (index, "y" * 3000),
        "raw_json": [{"type": "assistant", "message": {"role": "assistant",
                                                       "content": "hi"}}],
        "step_type": "implement",
    }


_BIG_LINE_CACHE = {}


def _big_line():
    """The oversized record serialised once and reused across tests."""
    if "line" not in _BIG_LINE_CACHE:
        _BIG_LINE_CACHE["line"] = json.dumps(
            _oversized_record(), ensure_ascii=False
        )
    return _BIG_LINE_CACHE["line"]


def _write_flow(root, flow_id, lines, name="01_discovery.jsonl"):
    jsonl = _flow_dir(root, flow_id) / name
    with jsonl.open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line)
            fh.write("\n")
    return jsonl


def _drain(reader, flow_id, cursor=None, max_rounds=200):
    """Read *flow_id* to exhaustion; return every record in delivery order."""
    out = []
    for _ in range(max_rounds):
        result = reader.read_flow(flow_id, cursor=cursor)
        out.extend(result.records)
        cursor = result.cursor
        if not result.truncated:
            return out
    pytest.fail("drain did not terminate")


def _raw_json_bytes(message):
    return len(json.dumps(message["raw_json"], ensure_ascii=False).encode("utf-8"))


def _chips(message):
    """(kind, id) of every tool_use / tool_result block, in document order."""
    found = []
    for event in message["raw_json"]:
        if not isinstance(event, dict):
            continue
        content = (event.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                found.append(("tool_use", block.get("id")))
            elif block.get("type") == "tool_result":
                found.append(("tool_result", block.get("tool_use_id")))
    return found


# --------------------------------------------------------------------------
# oversized record: budget met, nothing lost
# --------------------------------------------------------------------------


def test_oversized_record_is_delivered_within_the_raw_json_budget(tmp_path):
    big = _big_line()
    assert len(big.encode("utf-8")) > 16_000_000, "fixture is not 16 MB scale"
    _write_flow(tmp_path, "big", [json.dumps(_small_record(0)), big])

    records = _drain(_make_reader(tmp_path), "big")

    delivered = records[1]["message"]
    assert _raw_json_bytes(delivered) <= rb.MAX_RECORD_RAW_JSON_BYTES


def test_every_tool_chip_survives_compaction_in_order(tmp_path):
    _write_flow(tmp_path, "big", [_big_line()])

    records = _drain(_make_reader(tmp_path), "big")

    delivered = _chips(records[0]["message"])
    expected = _chips(_oversized_record())
    assert len(expected) == 2 * CHIP_COUNT
    assert delivered == expected, "compaction changed chip count or order"


def test_terminal_result_event_is_not_eaten_by_the_budget(tmp_path):
    """The tail is where a truncating degradation would lose events first."""
    _write_flow(tmp_path, "big", [_big_line()])

    records = _drain(_make_reader(tmp_path), "big")

    raw_json = records[0]["message"]["raw_json"]
    assert raw_json[-1]["type"] == "result"
    assert raw_json[-1]["usage"] == {"input_tokens": 49, "output_tokens": 14317}
    assert records[0]["message"]["usage_records"] == [
        {"model": "claude-opus-5", "output_tokens": 14317}
    ]


def test_shrunken_bodies_carry_a_truncation_marker(tmp_path):
    _write_flow(tmp_path, "big", [_big_line()])

    records = _drain(_make_reader(tmp_path), "big")

    marked = [
        event
        for event in records[0]["message"]["raw_json"]
        if isinstance(event, dict) and rb.TRUNCATION_FLAG_KEY in event
    ]
    assert marked, "no event reports having been shrunk"
    blob = json.dumps(marked[0], ensure_ascii=False)
    assert rb.TRUNCATION_MARKER_PATTERN.search(blob) or "[tianluo:truncated" in blob


def test_ordinals_are_unaffected_by_compaction(tmp_path):
    """``ordinal`` is a physical line number, compacted or not."""
    lines = [json.dumps(_small_record(i)) for i in range(3)]
    lines.insert(2, _big_line())
    _write_flow(tmp_path, "mix", lines)

    records = _drain(_make_reader(tmp_path), "mix")

    assert [r["ordinal"] for r in records] == list(range(len(lines)))
    assert _raw_json_bytes(records[2]["message"]) <= rb.MAX_RECORD_RAW_JSON_BYTES
    # The neighbours of the compacted line are untouched.
    assert records[1]["message"] == _small_record(1)
    assert records[3]["message"] == _small_record(2)


def test_compaction_is_logged_with_the_record_identity(tmp_path, caplog):
    _write_flow(tmp_path, "big", [_big_line()], name="03_implement.jsonl")

    with caplog.at_level(logging.INFO, logger="tianluo.daemon.history"):
        _drain(_make_reader(tmp_path), "big")

    lines = [r.getMessage() for r in caplog.records if "compacted oversized" in r.getMessage()]
    assert len(lines) == 1
    assert "flow=big" in lines[0]
    assert "step=03_implement" in lines[0]
    assert "ordinal=0" in lines[0]


# --------------------------------------------------------------------------
# the fast path stays a fast path
# --------------------------------------------------------------------------


def test_small_records_never_reach_the_compactor(tmp_path, monkeypatch):
    """Below the gate nothing is traversed — not even to decide it fits."""
    lines = [json.dumps(_small_record(i)) for i in range(40)]
    _write_flow(tmp_path, "small", lines)

    def _boom(*args, **kwargs):  # pragma: no cover - the assertion IS not calling it
        raise AssertionError("compact_record called for a sub-gate record")

    monkeypatch.setattr(history_mod, "compact_record", _boom)

    reader = _make_reader(tmp_path)
    records = _drain(reader, "small")
    # Second pass over the incremental branch, which has its own call site.
    first = reader.read_flow("small")
    reader.read_flow("small", cursor=first.cursor)

    assert len(records) == 40
    assert [r["message"] for r in records] == [_small_record(i) for i in range(40)]


def test_sub_budget_record_is_delivered_byte_identical(tmp_path):
    """Between the gate and the budget, a record with no oversized event is untouched."""
    record = _small_record(0)
    record["content"] = "z" * (200 * 1024)
    _write_flow(tmp_path, "mid", [json.dumps(record)])

    records = _drain(_make_reader(tmp_path), "mid")

    assert records[0]["message"] == record


# --------------------------------------------------------------------------
# one entry point for both read branches
# --------------------------------------------------------------------------


def test_both_read_branches_deliver_the_same_compacted_record(tmp_path):
    """A record's shape is a property of the record, not of the read that found it."""
    filler = [json.dumps(_small_record(i)) for i in range(120)]
    big_index = len(filler)
    lines = filler + [_big_line()]
    _write_flow(tmp_path, "both", lines)
    jsonl_name = "01_discovery.jsonl"

    # Branch 1 — incremental: the first (full-branch) round exhausts its byte
    # budget on the filler, so the big record arrives on a later seek-read.
    incremental_reader = _make_reader(tmp_path)
    first = incremental_reader.read_flow("both")
    assert first.truncated, "filler too small to push the big record past round 1"
    assert all(r["ordinal"] < big_index for r in first.records)
    incremental = None
    cursor = first.cursor
    for _ in range(200):
        result = incremental_reader.read_flow("both", cursor=cursor)
        for record in result.records:
            if record["ordinal"] == big_index:
                incremental = record
        cursor = result.cursor
        if incremental is not None or not result.truncated:
            break
    assert incremental is not None

    # Branch 2 — full read starting at the big record's line (a cold reader
    # whose caller cursor already points there).
    full_reader = _make_reader(tmp_path)
    full = full_reader.read_flow("both", cursor={jsonl_name: big_index})
    assert full.records[0]["ordinal"] == big_index

    assert full.records[0] == incremental


# --------------------------------------------------------------------------
# chunk accounting bills the compacted size
# --------------------------------------------------------------------------


def test_chunk_budget_bills_the_compacted_size(tmp_path):
    """A record that folds to a few KB must not cost a round-trip of its own.

    The telemetry-heavy record is multi-MB on disk and a few KB on the wire.
    Billing its raw size would trip the chunk cap immediately and strand the
    records behind it for another round; billing what actually ships lets the
    whole backlog leave in one frame.
    """
    heavy = json.dumps(_telemetry_heavy_record(), ensure_ascii=False)
    assert len(heavy.encode("utf-8")) > 4 * MAX_BYTES_PER_REPORT
    lines = [heavy] + [json.dumps(_small_record(i)) for i in range(3)]
    _write_flow(tmp_path, "fold", lines)

    result = _make_reader(tmp_path).read_flow("fold")

    assert not result.truncated, "the folded record still billed its raw size"
    assert [r["ordinal"] for r in result.records] == [0, 1, 2, 3]
    compacted = result.records[0]["message"]
    assert len(json.dumps(compacted, ensure_ascii=False).encode("utf-8")) < MAX_BYTES_PER_REPORT
    # Folding is not dropping: the chip pair and the result event survive.
    assert _chips(compacted) == [("tool_use", "toolu_0000"),
                                 ("tool_result", "toolu_0000")]
    assert compacted["raw_json"][-1]["type"] == "result"
