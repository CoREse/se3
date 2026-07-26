"""Tests for implement session splitting and timeline interleaving.

Covers:
- ``split_implement_session_by_iterations``: partition a single implement
  ChatSession into virtual per-iteration sessions using test session
  timestamps as fences.
- ``interleave_sessions_for_display``: reorder a flow's sessions so that
  virtual implement iterations appear chronologically between the test /
  self_check sessions of each fix loop round.

All fixtures construct ChatSession / ChatMessage objects in memory; no
filesystem or LLM is touched.
"""

from __future__ import annotations

from tianluo.engine.chat_history import (
    ChatMessage,
    ChatSession,
    interleave_sessions_for_display,
    split_implement_session_by_iterations,
)


def _msg(ts: str, content: str = "x", role: str = "user") -> ChatMessage:
    return ChatMessage(
        role=role,
        content=content,
        raw_json=[],
        timestamp=ts,
        step_type="implement",
        attempt=0,
    )


def _session(
    step_id: str,
    step_type: str,
    timestamps: list[str],
) -> ChatSession:
    return ChatSession(
        flow_id="flow1",
        step_id=step_id,
        step_type=step_type,
        messages=[_msg(ts) for ts in timestamps],
    )


# --- split_implement_session_by_iterations --------------------------------


def test_split_multi_round_returns_n_virtual_sessions():
    impl = _session(
        step_id="04_implement_abc12def",
        step_type="implement",
        timestamps=[
            "2026-04-17T10:00:00",  # iter1 prompt
            "2026-04-17T10:00:30",  # iter1 response
            "2026-04-17T10:05:00",  # iter2 prompt (after test-1)
            "2026-04-17T10:05:30",  # iter2 response
            "2026-04-17T10:10:00",  # iter3 prompt (after test-2)
        ],
    )
    test_ts = ["2026-04-17T10:02:00", "2026-04-17T10:07:00"]

    result = split_implement_session_by_iterations(impl, test_ts)

    assert [s.step_id for s in result] == [
        "04_implement_abc12def-iter1",
        "04_implement_abc12def-iter2",
        "04_implement_abc12def-iter3",
    ]
    assert [len(s.messages) for s in result] == [2, 2, 1]
    for s in result:
        assert s.flow_id == "flow1"
        assert s.step_type == "implement"


def test_split_single_round_returns_original_session():
    impl = _session(
        step_id="04_implement_abc",
        step_type="implement",
        timestamps=["2026-04-17T10:00:00", "2026-04-17T10:00:30"],
    )
    # Test timestamp is after all implement messages → single bucket.
    result = split_implement_session_by_iterations(
        impl, ["2026-04-17T10:05:00"]
    )

    assert result == [impl]
    assert result[0].step_id == "04_implement_abc"


def test_split_empty_session_returns_empty_list():
    impl = ChatSession(
        flow_id="flow1",
        step_id="04_implement_empty",
        step_type="implement",
        messages=[],
    )
    assert split_implement_session_by_iterations(impl, []) == []
    assert split_implement_session_by_iterations(
        impl, ["2026-04-17T10:00:00"]
    ) == []


def test_split_without_any_test_fences_returns_original():
    impl = _session(
        step_id="04_implement_xyz",
        step_type="implement",
        timestamps=["2026-04-17T10:00:00", "2026-04-17T10:01:00"],
    )
    result = split_implement_session_by_iterations(impl, [])
    assert result == [impl]


def test_split_preserves_message_order_within_bucket():
    impl = _session(
        step_id="04_implement_a",
        step_type="implement",
        timestamps=[
            "2026-04-17T10:00:00",
            "2026-04-17T10:00:10",
            "2026-04-17T10:00:20",
            "2026-04-17T10:05:00",
            "2026-04-17T10:05:10",
        ],
    )
    result = split_implement_session_by_iterations(
        impl, ["2026-04-17T10:03:00"]
    )

    assert len(result) == 2
    assert [m.timestamp for m in result[0].messages] == [
        "2026-04-17T10:00:00",
        "2026-04-17T10:00:10",
        "2026-04-17T10:00:20",
    ]
    assert [m.timestamp for m in result[1].messages] == [
        "2026-04-17T10:05:00",
        "2026-04-17T10:05:10",
    ]


def test_split_handles_unsorted_test_timestamps():
    impl = _session(
        step_id="04_implement_s",
        step_type="implement",
        timestamps=[
            "2026-04-17T10:00:00",
            "2026-04-17T10:05:00",
            "2026-04-17T10:10:00",
        ],
    )
    # Pass fences out-of-order; function must sort internally.
    result = split_implement_session_by_iterations(
        impl, ["2026-04-17T10:07:00", "2026-04-17T10:02:00"]
    )
    assert [len(s.messages) for s in result] == [1, 1, 1]


# --- interleave_sessions_for_display --------------------------------------


def test_interleave_produces_fix_loop_timeline():
    sessions = [
        _session(
            step_id="01_analyze_a",
            step_type="analyze",
            timestamps=["2026-04-17T09:00:00"],
        ),
        _session(
            step_id="02_plan_b",
            step_type="plan",
            timestamps=["2026-04-17T09:30:00"],
        ),
        _session(
            step_id="04_implement_c",
            step_type="implement",
            timestamps=[
                "2026-04-17T10:00:00",
                "2026-04-17T10:06:00",
                "2026-04-17T10:12:00",
            ],
        ),
        _session(
            step_id="05_test_d",
            step_type="test",
            timestamps=["2026-04-17T10:03:00"],
        ),
        _session(
            step_id="06_self_check_e",
            step_type="self_check",
            timestamps=["2026-04-17T10:04:00"],
        ),
        _session(
            step_id="07_test_f",
            step_type="test",
            timestamps=["2026-04-17T10:09:00"],
        ),
        _session(
            step_id="08_self_check_g",
            step_type="self_check",
            timestamps=["2026-04-17T10:10:00"],
        ),
        _session(
            step_id="09_test_h",
            step_type="test",
            timestamps=["2026-04-17T10:15:00"],
        ),
    ]

    result = interleave_sessions_for_display(sessions)

    assert [s.step_id for s in result] == [
        "01_analyze_a",
        "02_plan_b",
        "04_implement_c-iter1",
        "05_test_d",
        "06_self_check_e",
        "04_implement_c-iter2",
        "07_test_f",
        "08_self_check_g",
        "04_implement_c-iter3",
        "09_test_h",
    ]


def test_interleave_single_round_preserves_unchanged_implement():
    sessions = [
        _session(
            step_id="04_implement_c",
            step_type="implement",
            timestamps=["2026-04-17T10:00:00"],
        ),
        _session(
            step_id="05_test_d",
            step_type="test",
            timestamps=["2026-04-17T10:05:00"],
        ),
    ]

    result = interleave_sessions_for_display(sessions)

    assert [s.step_id for s in result] == ["04_implement_c", "05_test_d"]


def test_interleave_stable_sort_by_step_id_on_timestamp_tie():
    # Two non-implement sessions with identical first-message timestamps.
    # Ordering falls back to step_id (the stable tiebreaker).
    sessions = [
        _session(
            step_id="02_plan_b",
            step_type="plan",
            timestamps=["2026-04-17T09:00:00"],
        ),
        _session(
            step_id="01_analyze_a",
            step_type="analyze",
            timestamps=["2026-04-17T09:00:00"],
        ),
    ]

    result = interleave_sessions_for_display(sessions)
    assert [s.step_id for s in result] == ["01_analyze_a", "02_plan_b"]


def test_interleave_without_implement_acts_as_pure_sort():
    sessions = [
        _session(
            step_id="05_test_d",
            step_type="test",
            timestamps=["2026-04-17T10:05:00"],
        ),
        _session(
            step_id="01_analyze_a",
            step_type="analyze",
            timestamps=["2026-04-17T09:00:00"],
        ),
        _session(
            step_id="06_self_check_e",
            step_type="self_check",
            timestamps=["2026-04-17T10:06:00"],
        ),
    ]

    result = interleave_sessions_for_display(sessions)

    assert [s.step_id for s in result] == [
        "01_analyze_a",
        "05_test_d",
        "06_self_check_e",
    ]


def test_interleave_without_any_test_sessions_keeps_implement_whole():
    # No test sessions means no fence timestamps → implement session is
    # treated as single-round and emitted unchanged (no -iter1 suffix).
    sessions = [
        _session(
            step_id="01_analyze_a",
            step_type="analyze",
            timestamps=["2026-04-17T09:00:00"],
        ),
        _session(
            step_id="04_implement_c",
            step_type="implement",
            timestamps=[
                "2026-04-17T10:00:00",
                "2026-04-17T10:01:00",
            ],
        ),
    ]

    result = interleave_sessions_for_display(sessions)
    assert [s.step_id for s in result] == ["01_analyze_a", "04_implement_c"]


def test_interleave_empty_implement_session_is_dropped():
    # An implement session with no messages produces no virtual sessions.
    sessions = [
        ChatSession(
            flow_id="flow1",
            step_id="04_implement_empty",
            step_type="implement",
            messages=[],
        ),
        _session(
            step_id="05_test_d",
            step_type="test",
            timestamps=["2026-04-17T10:05:00"],
        ),
    ]

    result = interleave_sessions_for_display(sessions)
    assert [s.step_id for s in result] == ["05_test_d"]
