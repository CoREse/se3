"""Tests for :mod:`tianluo.daemon.record_budget`.

The fixtures deliberately mirror the shape of the record that motivated the
module: a ``discovery`` step record whose ``raw_json`` interleaves 206
``tool_use`` / ``tool_result`` pairs with tens of thousands of zero-render
``system/thinking_tokens`` telemetry events, plus at least one single event far
above the per-event cap.

The two assertions that matter most — and that any future rework of the
compaction algorithm has to keep passing — are that the tool chips come out with
identical count and order, and that the *tail* of the record survives. A
budget-exhaustion truncation would pass every size assertion here while silently
deleting the back half of a step's tool calls from the WebUI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tianluo.daemon import record_budget as rb


# ---------------------------------------------------------------- fixtures


def _thinking_event(index: int) -> dict:
    return {
        "type": "system",
        "subtype": "thinking_tokens",
        "estimated_tokens": 50 * index,
        "estimated_tokens_delta": 50,
        "uuid": "uuid-think-%d" % index,
        "session_id": "sess-1",
    }


def _tool_use_event(index: int, body: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "id": "msg-%d" % index,
            "role": "assistant",
            "model": "claude-opus-5",
            "content": [
                {"type": "thinking", "thinking": body, "signature": "sig-%d" % index},
                {
                    "type": "tool_use",
                    "id": "toolu_%04d" % index,
                    "name": "Bash",
                    "input": {"command": body},
                },
            ],
        },
    }


def _tool_result_event(index: int, body: str) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "tool_use_id": "toolu_%04d" % index,
                    "type": "tool_result",
                    "is_error": False,
                    "content": body,
                }
            ],
        },
    }


def _result_event() -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "total_cost_usd": 1.8261305,
        "usage": {
            "input_tokens": 49,
            "cache_creation_input_tokens": 64248,
            "cache_read_input_tokens": 1625655,
            "output_tokens": 14317,
        },
    }


CHIP_COUNT = 206


def _build_record(
    chips: int = CHIP_COUNT,
    body_bytes: int = 20 * 1024,
    thinking_per_chip: int = 20,
    oversized_chip: int = 7,
) -> dict:
    """A record shaped like the pathological 23.6 MB discovery record."""
    events = [{"type": "system", "subtype": "init", "cwd": "/tmp", "tools": ["Bash"]}]
    counter = 0
    for chip in range(chips):
        for _ in range(thinking_per_chip):
            counter += 1
            events.append(_thinking_event(counter))
        size = body_bytes if chip != oversized_chip else 400 * 1024
        body = ("payload-%d " % chip) * (size // 12)
        events.append(_tool_use_event(chip, "run step %d" % chip))
        events.append(_tool_result_event(chip, body))
    # The terminal events sit at the very end on purpose: they are what a
    # budget-exhaustion truncation would eat first.
    events.append(_thinking_event(counter + 1))
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


def _chips(message: dict) -> list:
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


# ---------------------------------------------------------------- constants


def test_thresholds_match_the_measured_distribution():
    assert rb.RECORD_FAST_PATH_BYTES == 64 * 1024
    assert rb.MAX_EVENT_BYTES == 256 * 1024
    assert rb.MAX_RECORD_RAW_JSON_BYTES == 1024 * 1024


@pytest.mark.parametrize(
    "raw_len,expected",
    [
        (0, False),
        (750, False),
        (36 * 1024, False),
        (rb.RECORD_FAST_PATH_BYTES - 1, False),
        (rb.RECORD_FAST_PATH_BYTES, True),
        (24 * 1024 * 1024, True),
    ],
)
def test_needs_compaction_boundary(raw_len, expected):
    assert rb.needs_compaction(raw_len) is expected


def test_module_has_no_daemon_internal_imports():
    """The offline script imports this module without dragging the daemon in."""
    source = Path(rb.__file__).read_text(encoding="utf-8")
    for forbidden in ("from .", "from tianluo.daemon", "import tianluo"):
        assert forbidden not in source


# ---------------------------------------------------------------- fast path


class _ExplodingList(list):
    """A raw_json stand-in that fails loudly if anything looks inside it."""

    def __iter__(self):  # pragma: no cover - failure path
        raise AssertionError("fast path traversed raw_json")

    def __len__(self):  # pragma: no cover - failure path
        raise AssertionError("fast path traversed raw_json")

    def __getitem__(self, item):  # pragma: no cover - failure path
        raise AssertionError("fast path traversed raw_json")


def test_fast_path_returns_the_same_object_without_traversing_raw_json():
    message = {"role": "assistant", "raw_json": _ExplodingList()}
    result, stats = rb.compact_record(message, raw_len=rb.RECORD_FAST_PATH_BYTES - 1)
    assert result is message
    assert stats.compacted is False
    assert stats.original_bytes == rb.RECORD_FAST_PATH_BYTES - 1


# ---------------------------------------------------------------- folding


def test_fold_collapses_measured_thinking_token_volume_to_thousands():
    events = []
    counter = 0
    runs = 500
    per_run = 46163 // runs
    for run in range(runs):
        for _ in range(per_run):
            counter += 1
            events.append(_thinking_event(counter))
        events.append(_tool_result_event(run, "body"))
    while counter < 46163:
        counter += 1
        events.append(_thinking_event(counter))

    folded, folded_count = rb.fold_telemetry_events(events)

    assert folded_count == 46163
    assert len(folded) < 2000
    assert sum(1 for e in folded if e.get("subtype") == rb.FOLDED_EVENT_SUBTYPE) == runs + 1
    assert sum(e.get("count", 0) for e in folded if e.get("subtype") == rb.FOLDED_EVENT_SUBTYPE) == 46163


def test_fold_leaves_assistant_user_and_result_events_untouched():
    events = [
        _tool_use_event(0, "a"),
        _thinking_event(1),
        _thinking_event(2),
        _tool_result_event(0, "b"),
        _result_event(),
    ]
    folded, folded_count = rb.fold_telemetry_events(events)

    assert folded_count == 2
    assert [e.get("type") for e in folded] == ["assistant", "system", "user", "result"]
    assert folded[0] is events[0]
    assert folded[2] is events[3]
    assert folded[3] is events[4]
    assert folded[1] == {
        "type": "system",
        "subtype": rb.FOLDED_EVENT_SUBTYPE,
        "count": 2,
        "kinds": ["system/thinking_tokens"],
    }


def test_fold_never_touches_types_outside_the_whitelist():
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "rate_limit_event", "rate_limit_info": {}},
        {"type": "system", "subtype": "task_started"},
        {"type": "system", "subtype": "task_notification"},
        {"type": "stream_event"},
    ]
    folded, folded_count = rb.fold_telemetry_events(events)

    assert folded_count == 0
    assert folded == events
    assert all(a is b for a, b in zip(folded, events))


def test_fold_of_a_pure_telemetry_record_keeps_one_marker():
    events = [_thinking_event(i) for i in range(10)]
    folded, folded_count = rb.fold_telemetry_events(events)
    assert folded_count == 10
    assert len(folded) == 1
    assert folded[0]["count"] == 10


def _bare_thinking_event(index: int) -> dict:
    """A telemetry event smaller than the marker that would replace it."""
    return {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": index}


def test_fold_leaves_a_run_alone_when_the_marker_would_be_bigger():
    """A run of one small telemetry event must not be 'folded' into growth."""
    events = [_bare_thinking_event(1)]
    marker = {
        "type": "system",
        "subtype": rb.FOLDED_EVENT_SUBTYPE,
        "count": 1,
        "kinds": ["system/thinking_tokens"],
    }
    assert rb.event_size(marker) > rb.event_size(events[0]), "fixture premise"

    folded, folded_count = rb.fold_telemetry_events(events)

    assert folded_count == 0
    assert folded[0] is events[0]


def test_fold_is_never_a_net_size_loss_when_telemetry_is_interleaved():
    events = []
    for index in range(200):
        events.append(_bare_thinking_event(index))
        events.append(_tool_result_event(index, "body %d" % index))

    folded, _ = rb.fold_telemetry_events(events)

    before = rb.event_size(events)
    assert rb.event_size(folded) <= before
    assert len(folded) == len(events)


# ---------------------------------------------------------------- watermark


def test_solve_watermark_returns_cap_when_everything_fits():
    assert rb.solve_watermark([10, 20, 30], budget=1000, cap=256) == 256


def test_solve_watermark_is_the_largest_feasible_level():
    sizes = [5, 5, 100, 900]
    budget = 300
    level = rb.solve_watermark(sizes, budget=budget, cap=10**9)
    assert sum(min(s, level) for s in sizes) <= budget
    assert sum(min(s, level + 1) for s in sizes) > budget


def test_solve_watermark_never_exceeds_the_per_event_cap():
    level = rb.solve_watermark([10**7], budget=10**9, cap=rb.MAX_EVENT_BYTES)
    assert level == rb.MAX_EVENT_BYTES


def test_solve_watermark_degenerate_inputs():
    assert rb.solve_watermark([], budget=10, cap=99) == 99
    assert rb.solve_watermark([100], budget=0, cap=99) == 0
    assert rb.solve_watermark([100], budget=-5, cap=99) == 0
    # Structural floor: many events, budget below their count → level 0, and the
    # caller (not this function) is responsible for reporting overflow.
    assert rb.solve_watermark([50] * 1000, budget=100, cap=99) == 0


# ---------------------------------------------------------------- shrinking


def test_shrink_event_returns_the_same_object_when_under_limit():
    event = _tool_result_event(1, "short body")
    result, dropped = rb.shrink_event(event, rb.MAX_EVENT_BYTES)
    assert result is event
    assert dropped == 0


def test_shrink_event_brings_an_oversized_event_within_the_cap():
    event = _tool_result_event(1, "x" * (600 * 1024))
    original = json.loads(json.dumps(event))

    result, dropped = rb.shrink_event(event, rb.MAX_EVENT_BYTES)

    assert rb.event_size(result) <= rb.MAX_EVENT_BYTES
    assert dropped > 0
    assert event == original, "shrink_event must not mutate its input"
    block = result["message"]["content"][0]
    assert block["tool_use_id"] == "toolu_0001"
    assert block["type"] == "tool_result"
    assert block["is_error"] is False
    assert result[rb.TRUNCATION_FLAG_KEY] == dropped


def test_truncation_marker_is_machine_recognisable():
    event = _tool_result_event(2, "y" * (600 * 1024))
    result, _ = rb.shrink_event(event, rb.MAX_EVENT_BYTES)
    body = result["message"]["content"][0]["content"]

    match = rb.TRUNCATION_MARKER_PATTERN.search(body)
    assert match is not None
    assert int(match.group(1)) > 0
    assert body.startswith("yyy")


def test_shrink_event_does_not_nest_markers_across_passes():
    event = _tool_result_event(3, "z" * (900 * 1024))
    once, _ = rb.shrink_event(event, rb.MAX_EVENT_BYTES)
    twice, _ = rb.shrink_event(once, 4096)
    body = twice["message"]["content"][0]["content"]
    assert body.count("[tianluo:truncated") == 1
    assert rb.event_size(twice) <= 4096


def test_shrink_event_reports_nothing_when_only_structural_noise_remains():
    event = {"type": "user", "message": {"content": [{"type": "text", "text": "hi"}] * 5}}
    result, dropped = rb.shrink_event(event, 4)
    assert result is event
    assert dropped == 0


# ---------------------------------------------------------------- compaction


def test_compact_record_preserves_every_tool_chip_count_and_order():
    message = _build_record()
    before = _chips(message)
    assert len(before) == CHIP_COUNT * 2

    compacted, stats = rb.compact_record(message, raw_len=rb.record_size(message))

    assert stats.compacted is True
    assert _chips(compacted) == before


def test_compact_record_never_drops_tail_events():
    message = _build_record()
    original_events = message["raw_json"]

    compacted, _ = rb.compact_record(message, raw_len=rb.record_size(message))
    events = compacted["raw_json"]

    # The terminal result event, and the last tool_result before it, are exactly
    # what a budget-exhaustion truncation would delete.
    assert events[-1] is original_events[-1]
    assert events[-1]["type"] == "result"
    last_chip = [e for e in events if (e.get("message") or {}).get("role") == "user"][-1]
    block = last_chip["message"]["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "toolu_%04d" % (CHIP_COUNT - 1)


def test_compact_record_meets_the_raw_json_budget():
    message = _build_record()
    compacted, stats = rb.compact_record(message, raw_len=rb.record_size(message))

    serialized = json.dumps(compacted["raw_json"], ensure_ascii=False)
    assert len(serialized.encode("utf-8")) <= rb.MAX_RECORD_RAW_JSON_BYTES
    assert stats.raw_json_bytes <= rb.MAX_RECORD_RAW_JSON_BYTES
    assert stats.overflow is False
    assert stats.compacted_bytes < stats.original_bytes


def test_compact_record_caps_every_event_at_the_per_event_ceiling():
    message = _build_record()
    compacted, _ = rb.compact_record(message, raw_len=rb.record_size(message))
    for event in compacted["raw_json"]:
        if rb.is_immune_event(event):
            continue
        assert rb.event_size(event) <= rb.MAX_EVENT_BYTES


def test_compact_record_leaves_usage_fields_byte_identical():
    message = _build_record()
    usage_records = message["usage_records"]
    token_usage = message["token_usage"]
    usage_json = json.dumps(usage_records, sort_keys=True)
    token_json = json.dumps(token_usage, sort_keys=True)

    compacted, _ = rb.compact_record(message, raw_len=rb.record_size(message))

    assert compacted["usage_records"] is usage_records
    assert compacted["token_usage"] is token_usage
    assert json.dumps(compacted["usage_records"], sort_keys=True) == usage_json
    assert json.dumps(compacted["token_usage"], sort_keys=True) == token_json
    assert compacted["content"] == message["content"]
    assert compacted["step_type"] == message["step_type"]
    assert compacted["timestamp"] == message["timestamp"]


def test_compact_record_leaves_the_result_event_untouched():
    message = _build_record()
    result_event = message["raw_json"][-1]

    compacted, _ = rb.compact_record(message, raw_len=rb.record_size(message))

    assert compacted["raw_json"][-1] is result_event
    assert compacted["raw_json"][-1]["usage"]["output_tokens"] == 14317


def test_compact_record_does_not_mutate_the_input_record():
    message = _build_record()
    original_events = list(message["raw_json"])
    original_count = len(original_events)

    rb.compact_record(message, raw_len=rb.record_size(message))

    assert message["raw_json"] is not None
    assert len(message["raw_json"]) == original_count
    assert message["raw_json"][0] is original_events[0]


def test_sub_budget_record_is_not_folded_but_oversized_event_is_capped():
    """64 KB–1 MB band: only the per-event cap may bite, telemetry stays intact."""
    message = {
        "role": "assistant",
        "raw_json": [
            _thinking_event(1),
            _thinking_event(2),
            _tool_result_event(0, "q" * (400 * 1024)),
            _thinking_event(3),
        ],
        "usage_records": [],
    }
    compacted, stats = rb.compact_record(message, raw_len=500 * 1024)

    assert stats.folded_events == 0
    assert stats.shrunk_events == 1
    assert [e.get("subtype") for e in compacted["raw_json"][:2]] == [
        "thinking_tokens",
        "thinking_tokens",
    ]
    assert rb.event_size(compacted["raw_json"][2]) <= rb.MAX_EVENT_BYTES
    assert len(compacted["raw_json"]) == 4


def test_compact_record_returns_input_when_nothing_needs_shrinking():
    message = {
        "role": "assistant",
        "raw_json": [_tool_result_event(0, "small body")],
    }
    compacted, stats = rb.compact_record(message, raw_len=70 * 1024)

    assert compacted is message
    assert stats.compacted is False
    assert stats.shrunk_events == 0


def test_compact_record_reports_overflow_instead_of_dropping_events():
    """Structural floor above budget: keep every event, flag the overshoot."""
    events = [
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "tool_use_id": "toolu_%05d" % index,
                        "type": "tool_result",
                        "is_error": False,
                        "content": "tiny",
                    }
                ],
            },
        }
        for index in range(12000)
    ]
    message = {"role": "assistant", "raw_json": events}

    compacted, stats = rb.compact_record(message, raw_len=2 * 1024 * 1024)

    assert len(compacted["raw_json"]) == len(events)
    assert stats.overflow is True
    # The level went below the events' own structural size, yet no event could
    # give anything up — every string in them is a label, not preview text.
    assert stats.watermark < rb.event_size(events[0])
    assert stats.shrunk_events == 0
    assert _chips(compacted) == _chips(message)


def test_compact_record_never_ships_a_bigger_record_than_it_was_given():
    """Interleaved one-event telemetry runs must not grow the record."""
    events = []
    for index in range(9000):
        events.append(_bare_thinking_event(index))
        events.append(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "delta %d " % index}],
                },
            }
        )
    message = {"role": "assistant", "raw_json": events}
    raw_len = rb.record_size(message)
    assert raw_len > rb.MAX_RECORD_RAW_JSON_BYTES, "fixture premise"

    compacted, stats = rb.compact_record(message, raw_len=raw_len)

    assert compacted is message
    assert stats.compacted is False
    assert stats.compacted_bytes == raw_len
    assert stats.folded_events == 0
    assert stats.raw_json_bytes <= raw_len
    # Still honestly reported as over budget, so the daemon logs the warning
    # rather than claiming a compaction that did not happen.
    assert stats.overflow is True


def test_compact_record_discards_a_pass_that_did_not_shrink_anything(monkeypatch):
    """Backstop: even a 'successful' shrink is dropped if the product grew."""
    message = _build_record(chips=4, body_bytes=400 * 1024, thinking_per_chip=2)
    raw_len = rb.record_size(message)

    def _inflating_shrink(event, limit):
        bloated = json.loads(json.dumps(event))
        bloated["padding"] = "z" * (2 * 1024 * 1024)
        return bloated, 1

    monkeypatch.setattr(rb, "shrink_event", _inflating_shrink)

    compacted, stats = rb.compact_record(message, raw_len=raw_len)

    assert compacted is message
    assert stats.compacted is False
    assert stats.compacted_bytes == raw_len
    assert stats.shrunk_events == 0
    assert stats.dropped_bytes == 0


def test_compact_record_handles_records_without_raw_json():
    message = {"role": "user", "content": "x" * (100 * 1024)}
    compacted, stats = rb.compact_record(message, raw_len=100 * 1024)
    assert compacted is message
    assert stats.compacted is False


def test_compacted_record_round_trips_through_json():
    message = _build_record()
    compacted, _ = rb.compact_record(message, raw_len=rb.record_size(message))
    reloaded = json.loads(json.dumps(compacted, ensure_ascii=False))
    assert _chips(reloaded) == _chips(compacted)
