"""Pure tests for provider-neutral usage parsing and aggregation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tianluo.usage import (
    CostSemantics,
    UsageRecord,
    UsageSemantics,
    UsageStatus,
    UsageSummary,
    aggregate_usage_records,
    deduplicate_usage_records,
    expand_configured_model,
    legacy_session_tally_is_authoritative,
    legacy_usage_record,
    parse_usage_record,
)
from tianluo.pricing import ModelPrice, PricingCatalog, TokenCategory


FIXTURES = Path(__file__).parent / "fixtures" / "usage"


def _result(usage, **extra):
    return {"type": "result", "usage": usage, **extra}


def test_usage_record_round_trip_preserves_null_semantics_and_diagnostics():
    record = UsageRecord(
        call_id="call-1",
        attempt=2,
        usage_status=UsageStatus.PARTIAL,
        agent_name="primary",
        runner_type="claude-code",
        provider="anthropic",
        provider_session_id="session-1",
        usage_event_id="event-1",
        reported_model="$ANTHROPIC_MODEL",
        resolved_model="unknown",
        logical_input_tokens=17,
        uncached_input_tokens=10,
        output_tokens=3,
        cache_read_input_tokens=4,
        cache_creation_5m_input_tokens=2,
        cache_creation_1h_input_tokens=1,
        actual_cost_usd=None,
        usage_semantics=UsageSemantics.CALL_DELTA,
        cost_semantics=CostSemantics.PROVIDER_SESSION_CUMULATIVE,
        usage_event_ids=["event-1", "event-2"],
        provider_session_ids=["session-1"],
        diagnostics=["cost unavailable"],
    )
    restored = UsageRecord.from_dict(json.loads(json.dumps(record.to_dict())))
    assert restored == record
    assert restored.actual_cost_usd is None
    assert restored.total_tokens == 20


def test_anthropic_inputs_are_mutually_exclusive_and_logical_total_is_provider_total():
    record = parse_usage_record(
        _result(
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 30,
                "cache_creation_input_tokens": 25,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 10,
                    "ephemeral_1h_input_tokens": 5,
                },
            },
            usage_event_id="anthropic-1",
        ),
        call_id="call-a",
        provider="anthropic",
    )
    assert record.usage_status == UsageStatus.AVAILABLE
    # Anthropic's input_tokens EXCLUDES the cache categories, so the reported
    # values are already mutually exclusive: uncached is input_tokens itself
    # and the logical input total is their sum.
    assert record.uncached_input_tokens == 100
    assert record.cache_read_input_tokens == 30
    assert record.cache_creation_input_tokens == 10
    assert record.cache_creation_5m_input_tokens == 10
    assert record.cache_creation_1h_input_tokens == 5
    assert record.logical_input_tokens == 155
    assert record.total_tokens == 175


def test_real_cache_heavy_claude_usage_is_available_and_additive():
    # The normal real-world shape: single-digit uncached input beside tens of
    # thousands of cached tokens. Treating input_tokens as a total containing
    # the cache would flag virtually every real Claude call partial and drop
    # the cache tokens out of the logical input total.
    record = parse_usage_record(
        _result(
            {
                "input_tokens": 4,
                "cache_creation_input_tokens": 24399,
                "cache_read_input_tokens": 14036,
                "output_tokens": 518,
            },
            usage_event_id="anthropic-cache-heavy",
        ),
        call_id="call-b",
        provider="anthropic",
    )
    assert record.usage_status == UsageStatus.AVAILABLE
    assert record.uncached_input_tokens == 4
    assert record.logical_input_tokens == 4 + 24399 + 14036
    assert record.total_tokens == 4 + 24399 + 14036 + 518
    assert record.diagnostics == []


@pytest.mark.parametrize(
    "details",
    [
        {"cached_input_tokens": 40},
        {"input_tokens_details": {"cached_tokens": 40}},
    ],
)
def test_openai_cached_input_is_a_subset(details):
    usage = {"input_tokens": 100, "output_tokens": 25, **details}
    record = parse_usage_record(
        _result(usage, usage_event_id="openai-1"),
        call_id="call-o",
        provider="openai",
    )
    assert record.logical_input_tokens == 100
    assert record.uncached_input_tokens == 60
    assert record.cache_read_input_tokens == 40
    assert record.total_tokens == 125


def test_explicit_zero_is_available_while_missing_usage_is_unavailable():
    zero = parse_usage_record(
        _result(
            {"input_tokens": 0, "output_tokens": 0},
            usage_event_id="zero",
            total_cost_usd=0,
        ),
        call_id="zero-call",
    )
    missing = parse_usage_record(
        {"type": "result", "result": "done"}, call_id="missing-call"
    )
    assert zero.usage_status == UsageStatus.AVAILABLE
    assert zero.actual_cost_usd == 0.0
    assert missing.usage_status == UsageStatus.UNAVAILABLE
    assert missing.actual_cost_usd is None


def test_malformed_usage_is_partial_not_synthetic_zero():
    record = parse_usage_record(
        {"type": "result", "usage": "bad"}, call_id="partial-call"
    )
    assert record.usage_status == UsageStatus.PARTIAL
    assert record.actual_cost_usd is None
    assert any("malformed usage" in item for item in record.diagnostics)


def test_usage_less_terminal_event_degrades_mixed_record_to_partial():
    # A multi-result invocation emits one result with usage, one failed result
    # with no usage fields, and one explicit zero — the missing measurement
    # must make the mixed record partial instead of silently available.
    events = [
        _result({"input_tokens": 10, "output_tokens": 1}, usage_event_id="a"),
        {"type": "result", "subtype": "error_max_turns", "result": "failed"},
        _result(
            {"input_tokens": 0, "output_tokens": 0}, usage_event_id="zero"
        ),
    ]
    record = parse_usage_record(events, call_id="mixed")
    assert record.logical_input_tokens == 10
    assert record.output_tokens == 1
    assert record.usage_status == UsageStatus.PARTIAL
    assert any("neither usage nor cost" in item for item in record.diagnostics)


def test_usage_less_only_terminal_event_stays_unavailable():
    # With no measured sibling at all, the call is still fully unavailable —
    # partial is reserved for a mixed record hiding a missing measurement.
    record = parse_usage_record(
        {"type": "result", "result": "done"}, call_id="missing-call"
    )
    assert record.usage_status == UsageStatus.UNAVAILABLE


def test_all_terminal_results_are_summed_and_duplicate_event_id_is_ignored():
    # A byte-identical re-emission of the same measurement id is a replay and
    # counts once.
    events = [
        _result(
            {"input_tokens": 10, "output_tokens": 1},
            usage_event_id="event-a",
        ),
        _result(
            {"input_tokens": 20, "output_tokens": 2},
            usage_event_id="event-b",
        ),
        _result(
            {"input_tokens": 10, "output_tokens": 1},
            usage_event_id="event-a",
        ),
    ]
    record = parse_usage_record(events, call_id="multi")
    assert record.logical_input_tokens == 30
    assert record.output_tokens == 3
    assert record.usage_event_ids == ["event-a", "event-b"]
    assert any("duplicate usage event" in item for item in record.diagnostics)


def test_differing_duplicate_event_id_replaces_with_later_snapshot():
    # A re-emission of the same measurement id carrying GROWN values is the
    # fuller report of that one measurement: it replaces the earlier values
    # and the record stays complete (only the diagnostic records the replay).
    events = [
        _result(
            {"input_tokens": 10, "output_tokens": 1},
            usage_event_id="event-a",
        ),
        _result(
            {"input_tokens": 20, "output_tokens": 2},
            usage_event_id="event-b",
        ),
        _result(
            {"input_tokens": 999, "output_tokens": 999},
            usage_event_id="event-a",
        ),
    ]
    record = parse_usage_record(events, call_id="multi")
    assert record.logical_input_tokens == 1019
    assert record.output_tokens == 1001
    assert record.usage_status == UsageStatus.AVAILABLE
    assert record.usage_event_ids == ["event-a", "event-b"]
    assert any(
        "later snapshot replaces the earlier one" in item
        for item in record.diagnostics
    )


def test_shrinking_duplicate_event_id_replay_marks_partial():
    # A re-emission that reports LESS than the retained values (without
    # cumulative semantics to reject it outright) loses measured tokens the
    # record can no longer account for, so it must not read as complete.
    events = [
        _result(
            {"input_tokens": 100, "output_tokens": 40},
            usage_event_id="event-a",
        ),
        _result(
            {"input_tokens": 10, "output_tokens": 4},
            usage_event_id="event-a",
        ),
    ]
    record = parse_usage_record(events, call_id="shrink")
    assert record.logical_input_tokens == 10
    assert record.usage_status == UsageStatus.PARTIAL


def test_usage_less_duplicate_event_id_keeps_measured_snapshot():
    # A re-emission carrying no measurement at all must not erase the
    # measured snapshot; the earlier values stay.
    events = [
        _result(
            {"input_tokens": 10, "output_tokens": 1},
            usage_event_id="event-a",
        ),
        _result({"result": "replayed without usage"}, usage_event_id="event-a"),
    ]
    record = parse_usage_record(events, call_id="keep")
    assert record.logical_input_tokens == 10
    assert record.output_tokens == 1
    assert any("duplicate usage event" in item for item in record.diagnostics)


def test_missing_event_id_uses_stable_attempt_local_key_and_deduplicates():
    event = _result({"input_tokens": 8, "output_tokens": 2}, result="same")
    record = parse_usage_record([event, dict(event)], call_id="synthetic")
    assert record.logical_input_tokens == 8
    assert len(record.usage_event_ids) == 1
    assert record.usage_event_ids[0].startswith("synthetic:")
    assert any("missing id" in item for item in record.diagnostics)


def test_distinct_idless_attempts_with_identical_payloads_are_kept():
    # Two real attempts through an id-less payload (compat proxy / older CLI
    # result format) that are byte-identical must not collapse: the synthetic
    # content key is attempt-local, not a global event identity.
    line = _result(
        {"input_tokens": 25, "output_tokens": 3},
        result="same failure text",
        total_cost_usd=0.001,
    )
    first = parse_usage_record(line, call_id="call-1", attempt=0)
    second = parse_usage_record(line, call_id="call-1", attempt=1)
    assert first.usage_event_ids[0].startswith("synthetic:")
    assert first.usage_event_ids == second.usage_event_ids
    aggregate = aggregate_usage_records([first, second])
    assert aggregate.logical_input_tokens == 50
    assert aggregate.output_tokens == 6
    assert aggregate.actual_cost_usd == pytest.approx(0.002)


def test_same_attempt_idless_replay_still_deduplicates():
    line = _result({"input_tokens": 25, "output_tokens": 3}, result="same")
    record = parse_usage_record(line, call_id="call-1", attempt=0)
    replay = UsageRecord.from_dict(record.to_dict())
    aggregate = aggregate_usage_records([record, replay])
    assert aggregate.logical_input_tokens == 25


def test_top_level_usage_covers_iterations_without_double_counting():
    record = parse_usage_record(
        {
            "type": "result",
            "usage_event_id": "outer",
            "usage": {"input_tokens": 30, "output_tokens": 3},
            "iterations": [
                {"usage": {"input_tokens": 10, "output_tokens": 1}},
                {"usage": {"input_tokens": 20, "output_tokens": 2}},
            ],
        },
        call_id="covered",
    )
    assert record.logical_input_tokens == 30
    assert record.output_tokens == 3


def test_iterations_and_subagents_are_scanned_when_top_level_has_no_usage():
    iterations = parse_usage_record(
        {
            "type": "result",
            "iterations": [
                {
                    "usage_event_id": "round-1",
                    "usage": {"input_tokens": 4, "output_tokens": 1},
                },
                {
                    "usage_event_id": "round-2",
                    "usage": {"input_tokens": 5, "output_tokens": 2},
                },
            ],
        },
        call_id="rounds",
    )
    subagents = parse_usage_record(
        {
            "type": "result",
            "subagents": [
                {
                    "usage_event_id": "sub-1",
                    "usage": {"input_tokens": 6, "output_tokens": 3},
                },
                {
                    "usage_event_id": "sub-2",
                    "usage": {"input_tokens": 7, "output_tokens": 4},
                },
            ],
        },
        call_id="subs",
    )
    assert (iterations.logical_input_tokens, iterations.output_tokens) == (9, 3)
    assert (subagents.logical_input_tokens, subagents.output_tokens) == (13, 7)


def test_cumulative_snapshots_keep_latest_values():
    record = parse_usage_record(
        [
            _result(
                {"input_tokens": 10, "output_tokens": 2},
                usage_event_id="snap-1",
                session_id="session-a",
                usage_semantics="provider_session_cumulative",
                cost_semantics="provider_session_cumulative",
                total_cost_usd=0.1,
            ),
            _result(
                {"input_tokens": 15, "output_tokens": 4},
                usage_event_id="snap-2",
                session_id="session-a",
                usage_semantics="provider_session_cumulative",
                cost_semantics="provider_session_cumulative",
                total_cost_usd=0.2,
            ),
        ],
        call_id="cumulative",
    )
    assert record.logical_input_tokens == 15
    assert record.output_tokens == 4
    assert record.actual_cost_usd == pytest.approx(0.2)


def test_non_monotonic_cumulative_snapshot_is_partial_with_diagnostic():
    record = parse_usage_record(
        [
            _result(
                {"input_tokens": 20, "output_tokens": 4},
                usage_event_id="snap-1",
                session_id="session-a",
                usage_semantics="provider_session_cumulative",
                cost_semantics="provider_session_cumulative",
                total_cost_usd=0.3,
            ),
            _result(
                {"input_tokens": 10, "output_tokens": 2},
                usage_event_id="snap-2",
                session_id="session-a",
                usage_semantics="provider_session_cumulative",
                cost_semantics="provider_session_cumulative",
                total_cost_usd=0.2,
            ),
        ],
        call_id="non-monotonic",
    )
    assert record.usage_status == UsageStatus.PARTIAL
    # The regression snapshot must not overwrite the trusted 0.30 total.
    assert record.actual_cost_usd == pytest.approx(0.3)
    # Token totals keep the same trusted snapshot the cost describes.
    assert record.logical_input_tokens == 20
    assert record.output_tokens == 4
    assert any("non-monotonic" in item for item in record.diagnostics)


def test_nested_total_cost_usd_declares_session_cumulative_for_anthropic():
    # A session-cumulative total sitting in a nested container the cost
    # extractor reads must be declared cumulative — otherwise the cumulative
    # series is summed as per-event deltas and the session over-billed.
    record = parse_usage_record(
        [
            {
                "type": "result",
                "message": {"total_cost_usd": 10.0},
                "session_id": "session-a",
                "provider": "anthropic",
                "usage": {"input_tokens": 10, "output_tokens": 1},
                "usage_event_id": "nested-snap-1",
            },
            {
                "type": "result",
                "message": {"total_cost_usd": 25.0},
                "session_id": "session-a",
                "provider": "anthropic",
                "usage": {"input_tokens": 20, "output_tokens": 2},
                "usage_event_id": "nested-snap-2",
            },
        ],
        call_id="nested-cumulative",
    )
    # Latest cost snapshot wins: 25.0, never the 35.0 blind sum. (Token
    # semantics are unaffected — those stay event-delta unless declared.)
    assert record.actual_cost_usd == pytest.approx(25.0)
    assert record.logical_input_tokens == 30
    assert record.output_tokens == 3


def test_openai_cache_creation_tokens_are_subtracted_from_uncached():
    # Cache-creation tokens are an input subset: leaving them inside
    # ``uncached`` while also pricing their own category double-bills them.
    record = parse_usage_record(
        _result(
            {
                "input_tokens": 1000,
                "output_tokens": 100,
                "cached_input_tokens": 300,
                "cache_creation_input_tokens": 100,
            },
            usage_event_id="openai-create",
            provider="openai",
        ),
        call_id="openai-create",
    )
    assert record.uncached_input_tokens == 600
    assert record.cache_read_input_tokens == 300
    assert record.cache_creation_input_tokens == 100
    # The priced categories sum back to the logical total — no double-billing.
    assert (
        record.uncached_input_tokens
        + record.cache_read_input_tokens
        + record.cache_creation_input_tokens
        == record.logical_input_tokens
        == 1000
    )


def test_regression_cost_snapshot_keeps_trusted_value_in_flow_aggregate():
    first = UsageRecord(
        call_id="call-1",
        attempt=0,
        usage_status=UsageStatus.AVAILABLE,
        provider="anthropic",
        provider_session_id="session-a",
        actual_cost_usd=0.30,
        cost_semantics=CostSemantics.PROVIDER_SESSION_CUMULATIVE,
    )
    regression = UsageRecord(
        call_id="call-2",
        attempt=1,
        usage_status=UsageStatus.PARTIAL,
        provider="anthropic",
        provider_session_id="session-a",
        actual_cost_usd=0.20,
        cost_semantics=CostSemantics.PROVIDER_SESSION_CUMULATIVE,
    )
    aggregate = aggregate_usage_records([first, regression])
    assert aggregate.actual_cost_usd == pytest.approx(0.30)
    assert aggregate.usage_status == UsageStatus.PARTIAL


def test_regression_token_record_keeps_trusted_value_in_flow_aggregate():
    first = UsageRecord(
        call_id="call-1",
        attempt=0,
        usage_status=UsageStatus.AVAILABLE,
        provider="anthropic",
        provider_session_id="session-a",
        logical_input_tokens=20,
        uncached_input_tokens=16,
        output_tokens=4,
        usage_semantics=UsageSemantics.PROVIDER_SESSION_CUMULATIVE,
    )
    regression = UsageRecord(
        call_id="call-2",
        attempt=1,
        usage_status=UsageStatus.PARTIAL,
        provider="anthropic",
        provider_session_id="session-a",
        logical_input_tokens=10,
        uncached_input_tokens=8,
        output_tokens=2,
        usage_semantics=UsageSemantics.PROVIDER_SESSION_CUMULATIVE,
    )
    aggregate = aggregate_usage_records([first, regression])
    # Tokens mirror the cost rule: the regression record must not overwrite
    # the trusted monotonic snapshot.
    assert aggregate.logical_input_tokens == 20
    assert aggregate.output_tokens == 4
    assert aggregate.usage_status == UsageStatus.PARTIAL


def test_overlapping_event_sets_are_deduplicated_per_event():
    partial = UsageRecord(
        call_id="call-1",
        attempt=0,
        usage_status=UsageStatus.AVAILABLE,
        provider="anthropic",
        provider_session_id="session-1",
        logical_input_tokens=10,
        uncached_input_tokens=10,
        output_tokens=1,
        usage_event_ids=["event-a"],
    )
    aggregate = UsageRecord(
        call_id="call-2",
        attempt=0,
        usage_status=UsageStatus.AVAILABLE,
        provider="anthropic",
        provider_session_id="session-1",
        logical_input_tokens=25,
        uncached_input_tokens=25,
        output_tokens=2,
        usage_event_ids=["event-a", "event-b"],
    )
    unique, _diagnostics = deduplicate_usage_records([partial, aggregate])
    # The aggregate already carries event-a's contribution; the partial record
    # is superseded instead of double-counting the shared event.
    assert [record.call_id for record in unique] == ["call-2"]
    total = aggregate_usage_records([partial, aggregate])
    assert total.logical_input_tokens == 25
    assert total.output_tokens == 2


def test_provider_session_cost_is_deduplicated_across_attempt_records():
    first = UsageRecord(
        call_id="call-1",
        attempt=0,
        usage_status=UsageStatus.AVAILABLE,
        provider="anthropic",
        provider_session_id="billing-session",
        logical_input_tokens=10,
        uncached_input_tokens=10,
        output_tokens=1,
        actual_cost_usd=0.10,
        cost_semantics=CostSemantics.PROVIDER_SESSION_CUMULATIVE,
    )
    second = UsageRecord(
        call_id="call-2",
        attempt=1,
        usage_status=UsageStatus.AVAILABLE,
        provider="anthropic",
        provider_session_id="billing-session",
        logical_input_tokens=20,
        uncached_input_tokens=20,
        output_tokens=2,
        actual_cost_usd=0.25,
        cost_semantics=CostSemantics.PROVIDER_SESSION_CUMULATIVE,
    )
    aggregate = aggregate_usage_records([first, second])
    assert aggregate.logical_input_tokens == 30
    assert aggregate.output_tokens == 3
    assert aggregate.actual_cost_usd == pytest.approx(0.25)


def test_duplicate_usage_record_does_not_double_count():
    record = UsageRecord(
        call_id="same-call",
        attempt=0,
        usage_status=UsageStatus.AVAILABLE,
        logical_input_tokens=10,
        uncached_input_tokens=10,
        output_tokens=2,
        usage_event_ids=["event-a"],
    )
    aggregate = aggregate_usage_records([record, record])
    assert aggregate.logical_input_tokens == 10
    assert aggregate.output_tokens == 2


def test_totals_and_summary_actual_cost_agree_on_breakdown_remainder():
    # A call whose cost_breakdown does not fully cover its call total leaves a
    # remainder — the call's own declared delta. The call's OWN snapshot must
    # not suppress its own delta (the record already added both in its
    # per-call cost), so ``totals``, the summary, and the per-call record all
    # report the same figure instead of silently dropping the declared delta.
    from tianluo.usage import SessionCostSnapshot

    record = UsageRecord(
        call_id="mixed-call",
        attempt=0,
        usage_status=UsageStatus.AVAILABLE,
        provider="anthropic",
        provider_session_id="s1",
        logical_input_tokens=100,
        uncached_input_tokens=100,
        output_tokens=10,
        actual_cost_usd=1.2,
        cost_semantics=CostSemantics.MIXED,
        cost_breakdown=[
            SessionCostSnapshot(
                provider="anthropic", provider_session_id="s1",
                actual_cost_usd=1.0,
            ),
        ],
    )
    summary = UsageSummary.summarize([record])
    assert summary.actual_cost_usd == pytest.approx(1.2)
    assert summary.totals.actual_cost_usd == pytest.approx(1.2)


def test_delta_cost_covered_by_session_snapshot_is_not_aggregated_twice():
    # Delta billing and a session-cumulative snapshot for the SAME provider
    # session: the delta is inside the snapshot. Both the billing units and
    # the totals aggregation must drop the covered delta.
    delta = UsageRecord(
        call_id="delta-call",
        attempt=0,
        usage_status=UsageStatus.AVAILABLE,
        provider="anthropic",
        provider_session_id="s1",
        logical_input_tokens=10,
        uncached_input_tokens=10,
        output_tokens=2,
        actual_cost_usd=0.3,
        cost_semantics=CostSemantics.EVENT_DELTA,
    )
    snapshot = UsageRecord(
        call_id="snapshot-call",
        attempt=0,
        usage_status=UsageStatus.AVAILABLE,
        provider="anthropic",
        provider_session_id="s1",
        logical_input_tokens=10,
        uncached_input_tokens=10,
        output_tokens=2,
        actual_cost_usd=1.0,
        cost_semantics=CostSemantics.PROVIDER_SESSION_CUMULATIVE,
    )
    summary = UsageSummary.summarize([delta, snapshot])
    assert summary.actual_cost_usd == pytest.approx(1.0)
    assert summary.totals.actual_cost_usd == pytest.approx(1.0)

    # A delta for an UNRELATED session still bills at the call level.
    other = UsageRecord(
        call_id="other-call",
        attempt=0,
        usage_status=UsageStatus.AVAILABLE,
        provider="anthropic",
        provider_session_id="s9",
        logical_input_tokens=5,
        uncached_input_tokens=5,
        output_tokens=1,
        actual_cost_usd=0.4,
        cost_semantics=CostSemantics.EVENT_DELTA,
    )
    summary = UsageSummary.summarize([delta, snapshot, other])
    assert summary.actual_cost_usd == pytest.approx(1.4)
    assert summary.totals.actual_cost_usd == pytest.approx(1.4)


def test_anthropic_shaped_usage_under_custom_provider_name_is_mutually_exclusive():
    # A compat proxy / wrapper command reports a custom provider string but an
    # Anthropic-shaped usage payload: normalization follows the payload's
    # shape, not the provider name, so the categories stay mutually exclusive
    # and additive exactly as for a declared-anthropic record.
    record = parse_usage_record(
        _result(
            {
                "input_tokens": 100,
                "output_tokens": 5,
                "cache_read_input_tokens": 30,
                "cache_creation_input_tokens": 10,
            },
            usage_event_id="proxy-1",
        ),
        call_id="proxy-call",
        provider="azure-anthropic",
    )
    assert record.provider == "azure-anthropic"
    assert record.usage_status == UsageStatus.AVAILABLE
    assert record.logical_input_tokens == 140
    assert record.uncached_input_tokens == 100
    assert record.cache_read_input_tokens == 30
    assert record.cache_creation_input_tokens == 10


def test_openai_shaped_usage_under_custom_provider_name_is_mutually_exclusive():
    # A compat proxy reporting a custom provider string but an OpenAI-shaped
    # payload: ``cached_input_tokens`` AND cache-creation categories are input
    # subsets — normalization follows the payload's shape, not the provider
    # name, so each priced category counts its tokens exactly once instead of
    # the 5 creation tokens being billed as uncached input AND cache creation.
    record = parse_usage_record(
        _result(
            {
                "input_tokens": 15,
                "cached_input_tokens": 10,
                "cache_creation_input_tokens": 5,
            },
            usage_event_id="proxy-openai-shape-1",
            provider="myproxy",
        ),
        call_id="proxy-openai-shape",
    )
    assert record.provider == "myproxy"
    assert record.usage_status == UsageStatus.AVAILABLE
    assert record.logical_input_tokens == 15
    assert record.uncached_input_tokens == 0
    assert record.cache_read_input_tokens == 10
    assert record.cache_creation_input_tokens == 5
    assert (
        record.uncached_input_tokens
        + record.cache_read_input_tokens
        + record.cache_creation_input_tokens
        == record.logical_input_tokens
    )


def test_within_call_delta_absorbed_by_later_session_snapshot():
    # One call's NDJSON carries a delta cost and a later same-session
    # cumulative snapshot that provably contains it: the record's cost must
    # be the snapshot (0.5), its breakdown must match its actual cost, and
    # the record must not read 0.8 while its breakdown says 0.5.
    record = parse_usage_record(
        [
            _result(
                {"input_tokens": 5, "output_tokens": 1},
                usage_event_id="within-delta",
                provider="anthropic",
                provider_session_id="abc",
                cost_semantics="event_delta",
                total_cost_usd=0.3,
            ),
            _result(
                {"input_tokens": 6, "output_tokens": 1},
                usage_event_id="within-snapshot",
                provider="anthropic",
                provider_session_id="abc",
                cost_semantics="provider_session_cumulative",
                total_cost_usd=0.5,
            ),
        ],
        call_id="within-call",
    )
    assert record.actual_cost_usd == pytest.approx(0.5)
    assert [
        (s.provider_session_id, s.actual_cost_usd)
        for s in record.cost_breakdown
    ] == [("abc", 0.5)]
    assert record.usage_status == UsageStatus.AVAILABLE
    assert any("covered" in d for d in record.diagnostics)


def test_within_call_delta_after_snapshot_is_counted():
    # The snapshot precedes the delta, so it cannot contain it: the record
    # bills both (0.5 snapshot + 0.2 delta), with the remainder kept
    # consistent with the breakdown.
    record = parse_usage_record(
        [
            _result(
                {"input_tokens": 6, "output_tokens": 1},
                usage_event_id="after-snapshot",
                provider="anthropic",
                provider_session_id="abc",
                cost_semantics="provider_session_cumulative",
                total_cost_usd=0.5,
            ),
            _result(
                {"input_tokens": 2, "output_tokens": 1},
                usage_event_id="after-delta",
                provider="anthropic",
                provider_session_id="abc",
                cost_semantics="event_delta",
                total_cost_usd=0.2,
            ),
        ],
        call_id="after-call",
    )
    assert record.actual_cost_usd == pytest.approx(0.7)
    assert [
        (s.provider_session_id, s.actual_cost_usd)
        for s in record.cost_breakdown
    ] == [("abc", 0.5)]


def _snapshot(event_id, tokens, cost):
    return _result(
        {"input_tokens": tokens, "output_tokens": 0},
        usage_event_id=event_id,
        provider="anthropic",
        provider_session_id="sess",
        usage_semantics="provider_session_cumulative",
        cost_semantics="provider_session_cumulative",
        total_cost_usd=cost,
    )


def _delta(event_id, tokens, cost):
    return _result(
        {"input_tokens": tokens, "output_tokens": 0},
        usage_event_id=event_id,
        provider="anthropic",
        provider_session_id="sess",
        usage_semantics="event_delta",
        cost_semantics="event_delta",
        total_cost_usd=cost,
    )


def test_reemitted_snapshot_absorbs_a_delta_that_arrived_before_it():
    # S1(100/$0.10, E1) → D(50/$0.05, E2) → S2(200/$0.20, re-emission of E1).
    # S2 replaces S1 in the id-ordered event list but ARRIVED after the delta
    # and provably contains it, so the delta must be absorbed: 200/$0.20, not
    # 250/$0.25. Absorption is a claim about arrival time, so a re-emission
    # must not be judged at its predecessor's position.
    record = parse_usage_record(
        [
            _snapshot("E1", 100, 0.10),
            _delta("E2", 50, 0.05),
            _snapshot("E1", 200, 0.20),
        ],
        call_id="reemit-absorb",
    )
    assert record.logical_input_tokens == 200
    assert record.actual_cost_usd == pytest.approx(0.20)
    assert any("covered by a later provider" in d for d in record.diagnostics)


def test_regressed_reemission_keeps_the_trusted_snapshot():
    # S2(200/$0.20, E1) → D(50/$0.05, E2) → S1(100/$0.10, re-emission of E1).
    # The re-emission REGRESSES below the retained cumulative value, so it must
    # be rejected rather than silently overwriting it — the per-scope monotonic
    # guards never see it, since only one entry per event id survives.
    record = parse_usage_record(
        [
            _snapshot("E1", 200, 0.20),
            _delta("E2", 50, 0.05),
            _snapshot("E1", 100, 0.10),
        ],
        call_id="reemit-regress",
    )
    # The snapshot precedes the delta, so it cannot contain it: both bill.
    assert record.logical_input_tokens == 250
    assert record.actual_cost_usd == pytest.approx(0.25)
    assert record.usage_status == UsageStatus.PARTIAL
    assert any("non-monotonic re-emission" in d for d in record.diagnostics)
    # The rejected re-emission must not be described as a replacement.
    assert not any(
        "later snapshot replaces the earlier one" in d
        for d in record.diagnostics
    )


def test_growing_reemission_still_replaces_the_earlier_values():
    record = parse_usage_record(
        [_snapshot("E1", 100, 0.10), _snapshot("E1", 200, 0.20)],
        call_id="reemit-grow",
    )
    assert record.logical_input_tokens == 200
    assert record.actual_cost_usd == pytest.approx(0.20)
    assert any(
        "later snapshot replaces the earlier one" in d
        for d in record.diagnostics
    )


def test_empty_usage_object_is_missing_data_not_an_explicit_zero():
    # A compat proxy emitting ``usage: {}`` reports no measurable field at all.
    # Presenting it as a measured zero would make an unmeasured call
    # indistinguishable from a real zero-consumption one.
    record = parse_usage_record(
        {"type": "result", "provider": "anthropic", "usage": {}},
        call_id="empty-usage",
    )
    assert record.usage_status == UsageStatus.PARTIAL
    assert record.total_tokens == 0
    assert any("no recognized token field" in d for d in record.diagnostics)

    # A payload that DOES declare its zeros stays a real measurement.
    explicit = parse_usage_record(
        _result({"input_tokens": 0, "output_tokens": 0}, total_cost_usd=0),
        call_id="explicit-zero",
    )
    assert explicit.usage_status == UsageStatus.AVAILABLE
    assert not any("no recognized token field" in d for d in explicit.diagnostics)


def test_unknown_model_count_matches_per_call_table_for_mixed_unit():
    # A billing unit aggregating one priced model and one unknown model: the
    # per-call table renders exactly ONE unknown-model row, so the summary
    # counter must be 1 — not the unit's "mixed" estimate failure counted on
    # top of the per-record count.
    priced = _record(
        "r1",
        model="claude-sonnet-4",
        session_id="s1",
        input_tokens=10,
        output_tokens=1,
        cost_semantics=CostSemantics.PROVIDER_SESSION_CUMULATIVE,
    )
    unknown = _record(
        "r2",
        model="unknown",
        session_id="s1",
        input_tokens=5,
        output_tokens=1,
        cost_semantics=CostSemantics.PROVIDER_SESSION_CUMULATIVE,
    )
    summary = UsageSummary.summarize([priced, unknown], catalog=CATALOG)
    assert summary.unknown_model_count == 1


def test_unknown_model_count_counts_unmappable_model_in_mixed_unit():
    # A billing unit aggregating one provenanced-but-unlisted model and one
    # unknown model: the per-call table renders TWO unknown-cost rows (the
    # unlisted name is real but absent from the catalog, the unknown record
    # has no model at all), so the summary counter must be 2 — the unit's
    # "mixed" estimate failure counts the unlisted model on top of the
    # per-record count, which only covers the unknown record.
    unlisted = _record(
        "r1",
        model="claude-opus-9-unlisted",
        session_id="s1",
        input_tokens=10,
        output_tokens=1,
        cost_semantics=CostSemantics.PROVIDER_SESSION_CUMULATIVE,
    )
    unknown = _record(
        "r2",
        model="unknown",
        session_id="s1",
        input_tokens=5,
        output_tokens=1,
        cost_semantics=CostSemantics.PROVIDER_SESSION_CUMULATIVE,
    )
    summary = UsageSummary.summarize([unlisted, unknown], catalog=CATALOG)
    assert summary.unknown_model_count == 2
    assert summary.completeness == "partial"


def test_unknown_model_count_counts_every_unlisted_model_in_one_unit():
    # A compat proxy / wrapper command can report several DIFFERENT model
    # names under one provider session; each unlisted name renders its own
    # unknown-cost row in the per-call table, so the unit-level count must
    # follow the rows instead of firing once for the whole unit.
    def unlisted(call_id: str, model: str):
        return _record(
            call_id,
            model=model,
            session_id="s1",
            input_tokens=10,
            output_tokens=1,
            cost_semantics=CostSemantics.PROVIDER_SESSION_CUMULATIVE,
        )

    two = [
        unlisted("r1", "claude-opus-9-unlisted"),
        unlisted("r2", "gpt-9-unlisted"),
    ]
    assert UsageSummary.summarize(two, catalog=CATALOG).unknown_model_count == 2

    unknown = _record(
        "r3",
        model="unknown",
        session_id="s1",
        input_tokens=5,
        output_tokens=1,
        cost_semantics=CostSemantics.PROVIDER_SESSION_CUMULATIVE,
    )
    summary = UsageSummary.summarize(two + [unknown], catalog=CATALOG)
    assert summary.unknown_model_count == 3
    assert summary.completeness == "partial"


def test_unknown_model_count_survives_a_units_known_actual_cost():
    # A compat proxy reporting a session-cumulative actual cost for a session
    # that also holds calls on an unlisted model: the unit never reaches
    # estimation (its cost is known), but the per-call table still renders the
    # cost-less row with no figure at all. The completeness label must not read
    # "complete" above that row.
    from tianluo.usage import build_usage_payload

    billed = _record(
        "r1",
        model="claude-opus-9-unlisted",
        session_id="s1",
        input_tokens=100,
        output_tokens=50,
        cost=1.0,
        cost_semantics=CostSemantics.PROVIDER_SESSION_CUMULATIVE,
    )
    unbilled = _record(
        "r2",
        model="claude-opus-9-unlisted",
        session_id="s1",
        input_tokens=100,
        output_tokens=50,
        cost_semantics=CostSemantics.PROVIDER_SESSION_CUMULATIVE,
    )
    summary = UsageSummary.summarize([billed, unbilled], catalog=CATALOG)
    assert summary.actual_cost_usd == pytest.approx(1.0)
    assert summary.unknown_model_count == 1
    assert summary.completeness == "partial"
    # The compact step-level summary reports the same unlisted model.
    assert (
        UsageSummary.summarize(
            [billed, unbilled], catalog=CATALOG, mark_unknown_models=False
        ).unknown_model_count
        == 1
    )

    payload = build_usage_payload({"implement": [billed, unbilled]}, CATALOG)
    costless = [
        call
        for call in payload["calls"]
        if call.get("actual_cost_usd") is None
        and call["estimated_cost_usd"] is None
    ]
    # The count describes exactly the rows that show no cost figure.
    assert len(costless) == payload["summary"]["unknown_model_count"] == 1
    assert payload["completeness"] == "partial"


def test_unlisted_model_with_its_own_actual_cost_is_not_unknown():
    # The price table's silence costs the reader nothing when the provider
    # itself billed the call: the row shows an exact actual cost.
    summary = UsageSummary.summarize(
        [
            _record(
                "r1",
                model="gpt-5-codex",
                provider="openai",
                input_tokens=300,
                output_tokens=60,
                cost=0.009,
            )
        ],
        catalog=CATALOG,
    )
    assert summary.actual_cost_usd == pytest.approx(0.009)
    assert summary.unknown_model_count == 0
    assert summary.completeness == "complete"


def test_mixed_listed_models_in_one_unit_are_not_unknown_models():
    # Caller rotation mid-step puts two DIFFERENT but catalog-listed models in
    # one provider-session unit. The aggregate's "mixed" model sentinel fails
    # estimation, which is honestly reported through ``partial`` — but no call
    # ran on an unknown model, so the count must stay 0 in both the per-call
    # payload path and the compact step-level summary persisted by the state
    # machine (``mark_unknown_models=False``).
    def listed(call_id: str, model: str):
        return _record(
            call_id,
            model=model,
            session_id="s1",
            input_tokens=100,
            output_tokens=50,
            cost_semantics=CostSemantics.PROVIDER_SESSION_CUMULATIVE,
        )

    records = [listed("r1", "claude-sonnet-4"), listed("r2", "claude-opus-5")]
    for mark in (True, False):
        summary = UsageSummary.summarize(
            records, catalog=CATALOG, mark_unknown_models=mark
        )
        assert summary.unknown_model_count == 0
        assert summary.partial is True


def test_unknown_call_count_counts_unavailable_row_in_measured_unit():
    # An UNAVAILABLE record sharing a session unit with a measured record
    # still renders as an unknown-usage row in the per-call table — the
    # completeness line must count it instead of reading "0 unknown calls"
    # above that row.
    unavailable = UsageRecord(
        call_id="u1",
        attempt=0,
        usage_status=UsageStatus.UNAVAILABLE,
        provider="anthropic",
        provider_session_id="abc",
        cost_semantics=CostSemantics.PROVIDER_SESSION_CUMULATIVE,
    )
    measured = UsageRecord(
        call_id="u2",
        attempt=0,
        usage_status=UsageStatus.AVAILABLE,
        provider="anthropic",
        provider_session_id="abc",
        logical_input_tokens=10,
        uncached_input_tokens=10,
        output_tokens=1,
        actual_cost_usd=0.01,
        cost_semantics=CostSemantics.PROVIDER_SESSION_CUMULATIVE,
    )
    summary = UsageSummary.summarize([unavailable, measured], catalog=CATALOG)
    assert summary.unknown_call_count == 1
    assert summary.actual_cost_usd == pytest.approx(0.01)


def test_breakdown_without_actual_cost_is_partial_and_surfaces_agree():
    # A persisted record whose cost_breakdown exists without an
    # actual_cost_usd is an inconsistent shape: it must read partial with a
    # diagnostic, and the summary and totals must derive from the SAME rule
    # (the breakdown snapshots) instead of one surface showing 0.9 while the
    # other shows None.
    record = UsageRecord.from_dict(
        {
            "call_id": "breakdown-only",
            "attempt": 0,
            "usage_status": "available",
            "cost_breakdown": [
                {
                    "provider": "anthropic",
                    "provider_session_id": "abc",
                    "actual_cost_usd": 0.9,
                }
            ],
            "actual_cost_usd": None,
        }
    )
    assert record.usage_status == UsageStatus.PARTIAL
    assert any("cost_breakdown" in d for d in record.diagnostics)
    summary = UsageSummary.summarize([record], catalog=CATALOG)
    assert summary.actual_cost_usd == pytest.approx(0.9)
    assert summary.totals.actual_cost_usd == pytest.approx(0.9)


def test_breakdown_exceeding_actual_cost_is_partial():
    # A persisted record whose breakdown snapshots sum ABOVE its call-level
    # actual cost contradicts the invariant that the breakdown is a subset of
    # the call cost (remainders are never negative). It must load partial
    # with a diagnostic so the disagreeing figures never pass as a complete
    # report — mirroring UsageEventAggregator.to_record's build-time check.
    record = UsageRecord.from_dict(
        {
            "call_id": "breakdown-over-actual",
            "attempt": 0,
            "usage_status": "available",
            "provider": "anthropic",
            "agent_name": "primary",
            "runner_type": "claude-code",
            "logical_input_tokens": 10,
            "uncached_input_tokens": 10,
            "output_tokens": 1,
            "resolved_model": "claude-sonnet-4-5",
            "cost_breakdown": [
                {
                    "provider": "anthropic",
                    "provider_session_id": "abc",
                    "actual_cost_usd": 0.9,
                }
            ],
            "actual_cost_usd": 0.4,
        }
    )
    assert record.usage_status == UsageStatus.PARTIAL
    assert any("cost_breakdown" in d for d in record.diagnostics)
    summary = UsageSummary.summarize([record], catalog=CATALOG)
    assert summary.completeness == "partial"

    # A breakdown below the actual cost is valid (the gap is the unabsorbed
    # remainder) and must stay AVAILABLE.
    valid = UsageRecord.from_dict(
        {
            "call_id": "breakdown-under-actual",
            "attempt": 0,
            "usage_status": "available",
            "provider": "anthropic",
            "agent_name": "primary",
            "runner_type": "claude-code",
            "logical_input_tokens": 10,
            "uncached_input_tokens": 10,
            "output_tokens": 1,
            "resolved_model": "claude-sonnet-4-5",
            "cost_breakdown": [
                {
                    "provider": "anthropic",
                    "provider_session_id": "abc",
                    "actual_cost_usd": 0.9,
                }
            ],
            "actual_cost_usd": 1.0,
        }
    )
    assert valid.usage_status == UsageStatus.AVAILABLE
    assert valid.diagnostics == []


def test_same_provider_event_is_deduplicated_across_ledger_copies():
    first = UsageRecord(
        call_id="history-copy",
        attempt=0,
        usage_status=UsageStatus.AVAILABLE,
        provider="openai",
        provider_session_id="thread-1",
        logical_input_tokens=10,
        uncached_input_tokens=10,
        usage_event_ids=["provider-event"],
    )
    second = UsageRecord.from_dict(first.to_dict())
    second.call_id = "step-copy"
    aggregate = aggregate_usage_records([first, second])
    assert aggregate.logical_input_tokens == 10


def test_legacy_adapter_marks_empty_as_ambiguous_and_missing_as_unavailable():
    missing = legacy_usage_record(None, call_id="missing")
    zero = legacy_usage_record(
        {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "total_cost_usd": 0,
        },
        call_id="zero",
    )
    nonzero = legacy_usage_record(
        {"input_tokens": 4, "output_tokens": 1, "total_cost_usd": 0.2},
        call_id="old",
    )
    assert missing.usage_status == UsageStatus.UNAVAILABLE
    assert zero.usage_status == UsageStatus.LEGACY_AMBIGUOUS
    assert zero.actual_cost_usd is None
    assert nonzero.usage_status == UsageStatus.AVAILABLE
    assert nonzero.provider is None
    assert nonzero.resolved_model == "unknown"


def test_legacy_adapter_projects_input_as_uncached_without_reinterpretation():
    record = legacy_usage_record(
        {
            "input_tokens": 100000,
            "output_tokens": 5000,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 30000,
        },
        call_id="legacy",
    )
    # Legacy tallies were accumulated from Anthropic-shaped events, whose
    # input_tokens excludes the cache categories.
    assert record.uncached_input_tokens == 100000
    assert record.cache_read_input_tokens == 30000
    # Unified token semantics: logical input = uncached + cache categories.
    assert record.logical_input_tokens == 130000
    assert record.logical_input_tokens == (
        record.uncached_input_tokens
        + record.cache_read_input_tokens
        + record.cache_creation_input_tokens
    )


def test_legacy_adapter_cache_heavy_tally_stays_available():
    # A real cache-heavy legacy tally (cache_read far exceeding input_tokens)
    # is the normal pre-upgrade shape — it must not be downgraded to PARTIAL
    # nor have its measured uncached input zeroed by a retroactive guess.
    record = legacy_usage_record(
        {
            "input_tokens": 199455537,
            "output_tokens": 5000,
            "cache_read_input_tokens": 669822679,
        },
        call_id="cache-heavy",
    )
    assert record.usage_status == UsageStatus.AVAILABLE
    assert record.uncached_input_tokens == 199455537
    assert record.logical_input_tokens == 199455537 + 669822679
    assert not any(
        "cache subsets exceed input_tokens" in item
        for item in record.diagnostics
    )


def test_legacy_uncached_split_prices_full_input_in_estimates():
    from tianluo.usage import estimate_record_cost

    record = legacy_usage_record(
        {
            "input_tokens": 100000,
            "output_tokens": 5000,
            "cache_read_input_tokens": 30000,
        },
        call_id="legacy",
    )
    record.resolved_model = "claude-sonnet-5"
    record.provider = "anthropic"
    estimate = estimate_record_cost(record, PricingCatalog.builtin())
    assert estimate.estimated_cost_usd is not None
    # 70k uncached input + 30k cache read + 5k output, priced at the builtin
    # sonnet-5 rates — the uncached majority is inside the estimate instead
    # of being silently excluded.
    priced = dict(estimate.priced)
    assert TokenCategory.UNCACHED_INPUT in priced
    assert TokenCategory.CACHE_READ in priced
    assert priced[TokenCategory.UNCACHED_INPUT] > priced[TokenCategory.CACHE_READ]


class TestLegacySessionTallyAuthority:
    """Which serialized states may still adapt their five-field tally.

    Presence of ``session_usage_records`` cannot decide this: the modern
    serializer adds an empty list to a re-saved pre-ledger flow. The
    structural invariant does — a modern tally is a PROJECTION of the ledger,
    so an empty ledger beside a non-zero tally can only be legacy data.
    """

    def test_pre_ledger_state_adapts(self):
        assert legacy_session_tally_is_authoritative(
            {"session_token_usage": {"input_tokens": 10, "output_tokens": 1}}
        )

    def test_pre_ledger_all_zero_tally_still_adapts(self):
        # The old synthesized all-zero tally must surface as legacy_ambiguous,
        # not be read as "no usage at all".
        assert legacy_session_tally_is_authoritative(
            {
                "session_token_usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_cost_usd": 0.0,
                }
            }
        )

    def test_round_tripped_non_zero_tally_stays_authoritative(self):
        assert legacy_session_tally_is_authoritative(
            {
                "session_usage_records": [],
                "session_token_usage": {
                    "input_tokens": 5000,
                    "output_tokens": 400,
                    "total_cost_usd": 1.23,
                },
            }
        )

    def test_modern_empty_ledger_with_zero_tally_is_not_legacy(self):
        assert not legacy_session_tally_is_authoritative(
            {
                "session_usage_records": [],
                "session_token_usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "total_cost_usd": 0.0,
                },
            }
        )

    def test_populated_ledger_is_not_legacy(self):
        assert not legacy_session_tally_is_authoritative(
            {
                "session_usage_records": [{"call_id": "c1"}],
                "session_token_usage": {"input_tokens": 5000},
            }
        )

    def test_embedded_projection_records_are_not_legacy(self):
        # An old projection that carried its own per-call records loads those
        # records as the ledger; the five-field block is then not the only fact.
        assert not legacy_session_tally_is_authoritative(
            {
                "session_token_usage": {
                    "input_tokens": 70,
                    "usage_records": [{"call_id": "c1"}],
                }
            }
        )

    def test_missing_or_malformed_tally_is_not_legacy(self):
        assert not legacy_session_tally_is_authoritative({})
        assert not legacy_session_tally_is_authoritative(
            {"session_usage_records": []}
        )
        assert not legacy_session_tally_is_authoritative(None)

    def test_unparsable_tally_value_counts_as_a_measurement(self):
        # Junk is surfaced (as a partial legacy record), never silently dropped.
        assert legacy_session_tally_is_authoritative(
            {
                "session_usage_records": [],
                "session_token_usage": {"input_tokens": "lots"},
            }
        )


def test_summary_completeness_reflects_missing_provenance():
    legacy = legacy_usage_record(
        {"input_tokens": 10, "output_tokens": 1, "total_cost_usd": 0.001},
        call_id="legacy",
    )
    summary = UsageSummary.summarize([legacy], PricingCatalog.builtin())
    assert summary.unknown_model_count == 1
    assert summary.completeness == "partial"

    modern = UsageRecord(
        call_id="modern",
        attempt=0,
        usage_status=UsageStatus.AVAILABLE,
        agent_name="primary",
        runner_type="claude-code",
        provider="anthropic",
        resolved_model="claude-sonnet-5",
        logical_input_tokens=10,
        uncached_input_tokens=10,
        output_tokens=1,
    )
    summary = UsageSummary.summarize([modern], PricingCatalog.builtin())
    assert summary.unknown_model_count == 0
    assert summary.completeness == "complete"


def test_init_metadata_and_terminal_model_are_retained():
    record = parse_usage_record(
        [
            {
                "type": "init",
                "provider": "anthropic",
                "session_id": "provider-session",
                "model": "claude-provider-model",
            },
            _result(
                {"input_tokens": 1, "output_tokens": 2},
                usage_event_id="event",
            ),
        ],
        call_id="metadata",
        agent_name="configured-agent",
        runner_type="claude-code",
        reported_model="$ANTHROPIC_MODEL",
        resolved_model="unknown",
    )
    assert record.agent_name == "configured-agent"
    assert record.runner_type == "claude-code"
    assert record.provider == "anthropic"
    assert record.provider_session_id == "provider-session"
    assert record.reported_model == "claude-provider-model"
    assert record.resolved_model == "claude-provider-model"
    assert record.resolved_model_source == "provider"


def test_model_resolution_priority_and_source_markers(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "Claude-Configured-Model")
    configured = expand_configured_model("$AGENT_MODEL")

    provider = parse_usage_record(
        [
            {"type": "init", "model": "Claude-Provider-Model"},
            _result({"input_tokens": 1, "output_tokens": 1}),
        ],
        configured_model=configured,
        runner_startup_model="Claude-Runner-Model",
    )
    agent_config = parse_usage_record(
        _result({"input_tokens": 1, "output_tokens": 1}),
        configured_model=configured,
        runner_startup_model="Claude-Runner-Model",
    )
    startup = parse_usage_record(
        _result({"input_tokens": 1, "output_tokens": 1}),
        runner_startup_model="Claude-Runner-Model",
    )
    unknown = parse_usage_record(
        _result({"input_tokens": 1, "output_tokens": 1})
    )

    assert provider.reported_model == "Claude-Provider-Model"
    assert (provider.resolved_model, provider.resolved_model_source) == (
        "claude-provider-model",
        "provider",
    )
    assert (agent_config.resolved_model, agent_config.resolved_model_source) == (
        "claude-configured-model",
        "agent_config",
    )
    assert (startup.resolved_model, startup.resolved_model_source) == (
        "claude-runner-model",
        "runner_startup",
    )
    assert (unknown.resolved_model, unknown.resolved_model_source) == (
        "unknown",
        "unknown",
    )


def test_unexpanded_model_literal_never_becomes_resolved(monkeypatch):
    monkeypatch.delenv("MISSING_AGENT_MODEL", raising=False)
    configured = expand_configured_model("${MISSING_AGENT_MODEL}")
    record = parse_usage_record(
        [
            {"type": "init", "model": "$PROVIDER_MODEL"},
            _result({"input_tokens": 1, "output_tokens": 0}),
        ],
        configured_model=configured,
        runner_startup_model="$RUNNER_MODEL",
    )
    assert configured is None
    assert record.reported_model == "$PROVIDER_MODEL"
    assert record.resolved_model == "unknown"
    assert "$" not in record.resolved_model


@pytest.mark.parametrize(
    "provider_event",
    [
        {"type": "init", "model": "Provider-Init-Model"},
        {"type": "system", "model": "Provider-System-Model"},
        {
            "type": "assistant",
            "message": {"model": "Provider-Stream-Model", "content": []},
        },
        {
            "type": "result",
            "model": "Provider-Result-Model",
            "usage": {"input_tokens": 1, "output_tokens": 0},
        },
    ],
)
def test_any_explicit_provider_model_layer_beats_fallbacks(provider_event):
    events = [provider_event]
    if provider_event["type"] != "result":
        events.append(_result({"input_tokens": 1, "output_tokens": 0}))
    record = parse_usage_record(
        events,
        configured_model="Configured-Model",
        runner_startup_model="Runner-Model",
    )
    expected = (
        provider_event.get("model")
        or provider_event.get("message", {}).get("model")
    )
    assert record.reported_model == expected
    assert record.resolved_model == expected.lower()
    assert record.resolved_model_source == "provider"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "name",
    [
        "claude_print.jsonl",
        "claude_goal_direct.jsonl",
        "claude_goal_unavailable.jsonl",
        "claude_subagent.jsonl",
        "claude_multi_turn.jsonl",
        "failure_and_missing_usage.jsonl",
    ],
)
def test_fixture_parser_matches_live_stream_tracker(name, capsys):
    from tianluo.engine.llm_caller import StreamJSONTracker

    raw = _fixture(name)
    metadata = {
        "call_id": f"fixture:{name}",
        "attempt": 0,
        "agent_name": "fixture-agent",
        "runner_type": "claude-code",
        "provider": "anthropic",
        "configured_model": "configured-fallback",
        "runner_startup_model": "runner-fallback",
    }
    parsed = parse_usage_record(raw, **metadata)
    tracker = StreamJSONTracker(
        call_id=metadata["call_id"],
        usage_attempt=0,
        agent_name=metadata["agent_name"],
        runner_type=metadata["runner_type"],
        provider=metadata["provider"],
        configured_model=metadata["configured_model"],
        runner_startup_model=metadata["runner_startup_model"],
    )
    for line in raw.splitlines():
        tracker.process_line(line)
    capsys.readouterr()
    assert tracker.usage_record == parsed


def test_claude_multi_turn_fixture_uses_delta_tokens_and_latest_session_cost():
    record = parse_usage_record(
        _fixture("claude_multi_turn.jsonl"),
        provider="anthropic",
    )
    assert record.logical_input_tokens == 250
    assert record.output_tokens == 55
    assert record.actual_cost_usd == pytest.approx(0.008)
    assert record.usage_event_ids == ["claude-turn-1", "claude-turn-2"]


def test_failure_fixture_distinguishes_missing_usage_from_explicit_zero():
    events = [
        json.loads(line)
        for line in _fixture("failure_and_missing_usage.jsonl").splitlines()
    ]
    missing = parse_usage_record(events[2], call_id="missing")
    explicit_zero = parse_usage_record(events[3], call_id="zero")
    assert missing.usage_status == UsageStatus.UNAVAILABLE
    assert missing.actual_cost_usd is None
    assert explicit_zero.usage_status == UsageStatus.AVAILABLE
    assert explicit_zero.actual_cost_usd == 0.0


# ---------------------------------------------------------------------------
# UsageSummary: billing-unit de-duplication, estimation, and classification
# ---------------------------------------------------------------------------


def _record(
    call_id,
    *,
    model="claude-opus-5",
    provider="anthropic",
    session_id=None,
    input_tokens=0,
    output_tokens=0,
    cache_read=0,
    cache_creation_5m=0,
    cost=None,
    cost_semantics=CostSemantics.EVENT_DELTA,
    status=UsageStatus.AVAILABLE,
):
    return UsageRecord(
        call_id=call_id,
        attempt=0,
        usage_status=status,
        provider=provider,
        provider_session_id=session_id,
        resolved_model=model,
        logical_input_tokens=input_tokens + cache_read + cache_creation_5m,
        uncached_input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_5m_input_tokens=cache_creation_5m,
        actual_cost_usd=cost,
        cost_semantics=cost_semantics,
    )


CATALOG = PricingCatalog.builtin()


def test_summary_actual_only_flow_is_complete():
    summary = UsageSummary.summarize(
        [_record("c1", input_tokens=100, output_tokens=10, cost=0.001)],
        catalog=CATALOG,
    )
    assert summary.actual_cost_usd == pytest.approx(0.001)
    # A unit with actual cost is never also estimated.
    assert summary.estimated_cost_usd is None
    assert summary.completeness == "complete"
    assert summary.totals.total_tokens == 110


def test_summary_estimate_only_flow_uses_token_pricing():
    summary = UsageSummary.summarize(
        [_record("c1", input_tokens=1_000_000, output_tokens=1_000_000)],
        catalog=CATALOG,
    )
    assert summary.actual_cost_usd is None
    assert summary.estimated_cost_usd == pytest.approx(5.0 + 25.0)
    assert summary.completeness == "complete"


def test_summary_mixed_actual_and_estimated_stay_separate():
    records = [
        _record("c1", input_tokens=100, output_tokens=10, cost=0.001),
        _record("c2", input_tokens=1_000, output_tokens=100),
    ]
    summary = UsageSummary.summarize(records, catalog=CATALOG)
    assert summary.actual_cost_usd == pytest.approx(0.001)
    # Only the unit WITHOUT actual cost is estimated (1000 in + 100 out).
    assert summary.estimated_cost_usd == pytest.approx(
        (1000 * 5.0 + 100 * 25.0) / 1e6
    )
    assert summary.completeness == "complete"


def test_summary_fully_unknown_flow_counts_unknown_calls():
    summary = UsageSummary.summarize(
        [
            _record("c1", status=UsageStatus.UNAVAILABLE),
            _record("c2", status=UsageStatus.UNAVAILABLE),
        ],
        catalog=CATALOG,
    )
    assert summary.unknown_call_count == 2
    assert summary.actual_cost_usd is None
    assert summary.estimated_cost_usd is None
    assert summary.completeness == "partial"


def test_summary_unknown_model_is_not_priced_as_zero():
    summary = UsageSummary.summarize(
        [_record("c1", model="unknown", input_tokens=100, output_tokens=10)],
        catalog=CATALOG,
    )
    assert summary.estimated_cost_usd is None
    assert summary.unknown_model_count == 1
    assert summary.completeness == "partial"


def test_summary_unknown_cache_ttl_price_is_reported_separately():
    # gpt-5 has input/output/cache_read prices but no cache-creation TTLs.
    record = UsageRecord(
        call_id="c1",
        attempt=0,
        usage_status=UsageStatus.AVAILABLE,
        provider="anthropic",
        resolved_model="gpt-5",
        logical_input_tokens=150,
        uncached_input_tokens=100,
        cache_read_input_tokens=20,
        cache_creation_5m_input_tokens=30,
        output_tokens=10,
    )
    summary = UsageSummary.summarize([record], catalog=CATALOG)
    assert summary.estimated_cost_usd is None
    assert summary.unknown_cache_ttl_count == 1
    assert summary.unknown_price_count == 0


def test_summary_missing_base_price_is_unknown_price():
    # cache_read-only entry: input/output carry tokens but have no price.
    catalog = PricingCatalog.builtin().with_overrides(
        {"claude-opus-5": {"input": 5.0, "output": 25.0}}
    )
    record = UsageRecord(
        call_id="c1",
        attempt=0,
        usage_status=UsageStatus.AVAILABLE,
        provider="anthropic",
        resolved_model="claude-opus-5",
        logical_input_tokens=100,
        uncached_input_tokens=90,
        cache_read_input_tokens=10,
        output_tokens=10,
    )
    catalog = PricingCatalog(
        entries={
            "claude-opus-5": ModelPrice(
                model="claude-opus-5", cache_read=0.5
            )
        }
    )
    summary = UsageSummary.summarize([record], catalog=catalog)
    assert summary.estimated_cost_usd is None
    assert summary.unknown_price_count == 1


def test_summary_no_catalog_means_unknown_price_not_zero():
    summary = UsageSummary.summarize(
        [_record("c1", input_tokens=100, output_tokens=10)],
        catalog=None,
    )
    assert summary.estimated_cost_usd is None
    assert summary.unknown_price_count == 1


def test_session_cumulative_cost_uses_latest_valid_monotonic_snapshot():
    records = [
        _record(
            "c1",
            session_id="session-1",
            input_tokens=50,
            output_tokens=5,
            cost=0.010,
            cost_semantics=CostSemantics.PROVIDER_SESSION_CUMULATIVE,
        ),
        _record(
            "c2",
            session_id="session-1",
            input_tokens=60,
            output_tokens=6,
            cost=0.018,
            cost_semantics=CostSemantics.PROVIDER_SESSION_CUMULATIVE,
        ),
    ]
    summary = UsageSummary.summarize(records, catalog=CATALOG)
    # One billing unit: the latest snapshot wins, earlier one not added.
    assert summary.actual_cost_usd == pytest.approx(0.018)
    assert not summary.partial


def test_non_monotonic_snapshot_keeps_trusted_value_and_flags_partial():
    records = [
        _record(
            "c1",
            session_id="session-1",
            cost=0.010,
            cost_semantics=CostSemantics.PROVIDER_SESSION_CUMULATIVE,
        ),
        _record(
            "c2",
            session_id="session-1",
            cost=0.008,  # regression snapshot — must not overwrite
            cost_semantics=CostSemantics.PROVIDER_SESSION_CUMULATIVE,
        ),
    ]
    summary = UsageSummary.summarize(records, catalog=CATALOG)
    assert summary.actual_cost_usd == pytest.approx(0.010)
    assert summary.partial
    assert any("non-monotonic" in d for d in summary.diagnostics)


def test_multi_session_call_keeps_billing_identity_for_later_snapshot():
    # One call reports cumulative costs for sessions A and B; a later call
    # reports a newer snapshot for A only. The summary must take the latest
    # snapshot PER session (0.15 + 0.20), not sum the frozen call-level mix
    # with the newer A snapshot (0.10 + 0.20 + 0.15).
    first = parse_usage_record(
        [
            {
                "type": "result",
                "provider": "anthropic",
                "provider_session_id": "A",
                "cost_semantics": "provider_session_cumulative",
                "total_cost_usd": 0.10,
                "usage_event_id": "call1-a",
                "usage": {"input_tokens": 10, "output_tokens": 1},
            },
            {
                "type": "result",
                "provider": "anthropic",
                "provider_session_id": "B",
                "cost_semantics": "provider_session_cumulative",
                "total_cost_usd": 0.20,
                "usage_event_id": "call1-b",
                "usage": {"input_tokens": 10, "output_tokens": 1},
            },
        ],
        call_id="call-1",
        provider="anthropic",
    )
    assert first.actual_cost_usd == pytest.approx(0.30)
    assert [(s.provider_session_id, s.actual_cost_usd) for s in first.cost_breakdown] == [
        ("A", 0.10), ("B", 0.20),
    ]
    later = parse_usage_record(
        _result(
            {"input_tokens": 5, "output_tokens": 1},
            provider="anthropic",
            provider_session_id="A",
            cost_semantics="provider_session_cumulative",
            total_cost_usd=0.15,
            usage_event_id="call2-a",
        ),
        call_id="call-2",
        provider="anthropic",
    )
    summary = UsageSummary.summarize([first, later], catalog=CATALOG)
    assert summary.actual_cost_usd == pytest.approx(0.35)
    assert summary.totals.actual_cost_usd == pytest.approx(0.35)
    assert not summary.partial
    # The breakdown survives a serialization round-trip.
    restored = UsageRecord.from_dict(first.to_dict())
    assert len(restored.cost_breakdown) == 2


def test_delta_costs_sum_across_sessions():
    records = [
        _record("c1", session_id="session-1", cost=0.001),
        _record("c2", session_id="session-2", cost=0.002),
        _record("c3", session_id="session-1", cost=0.004),
    ]
    summary = UsageSummary.summarize(records, catalog=CATALOG)
    assert summary.actual_cost_usd == pytest.approx(0.007)


def test_explicit_zero_usage_stays_available_not_unknown():
    zero = UsageRecord(
        call_id="zero-call",
        attempt=0,
        usage_status=UsageStatus.AVAILABLE,
        provider="openai",
        provider_session_id="thread-1",
        resolved_model="gpt-5.1-codex",
    )
    summary = UsageSummary.summarize([zero], catalog=CATALOG)
    # Explicitly reported zero consumption is a real measurement — not a
    # missing-usage call and not a partial estimate.
    assert summary.unknown_call_count == 0
    assert summary.actual_cost_usd is None
    assert summary.estimated_cost_usd is None
    assert not summary.partial
    assert summary.completeness == "complete"
    assert summary.totals.usage_status == UsageStatus.AVAILABLE


def test_cross_provider_same_session_string_is_not_suppressed():
    anthropic_snapshot = _record(
        "c1",
        provider="anthropic",
        session_id="abc",
        cost=0.30,
        cost_semantics=CostSemantics.PROVIDER_SESSION_CUMULATIVE,
    )
    openai_delta = _record(
        "c2",
        provider="openai",
        session_id="abc",
        cost=0.05,
    )
    summary = UsageSummary.summarize(
        [anthropic_snapshot, openai_delta], catalog=CATALOG
    )
    # Session ids are only unique within one provider: the OpenAI delta is NOT
    # inside the Anthropic snapshot and must not be dropped.
    assert summary.actual_cost_usd == pytest.approx(0.35)


def test_delta_after_session_snapshot_bills_per_delta_semantics():
    # A cumulative snapshot emitted BEFORE a later delta for the same session
    # cannot contain that delta: without evidence that the snapshot is later
    # than the delta and covers its magnitude, the delta must be counted —
    # "a snapshot exists" alone is not proof of containment.
    cumulative = _record(
        "c1",
        session_id="session-1",
        cost=0.010,
        cost_semantics=CostSemantics.PROVIDER_SESSION_CUMULATIVE,
    )
    delta = _record("c2", session_id="session-1", cost=0.001)
    summary = UsageSummary.summarize([cumulative, delta], catalog=CATALOG)
    assert summary.actual_cost_usd == pytest.approx(0.011)
    assert summary.totals.actual_cost_usd == pytest.approx(0.011)
    assert not summary.partial
    assert not any("covered" in d for d in summary.diagnostics)


def test_delta_after_small_snapshot_bills_even_though_a_snapshot_exists():
    # The snapshot exists but is earlier than the delta AND smaller than it:
    # dropping the delta would undercount the true cost (0.5 + 2.0).
    snapshot = _record(
        "b",
        session_id="abc",
        cost=0.5,
        cost_semantics=CostSemantics.PROVIDER_SESSION_CUMULATIVE,
    )
    later_delta = _record("c", session_id="abc", cost=2.0)
    summary = UsageSummary.summarize([snapshot, later_delta], catalog=CATALOG)
    assert summary.actual_cost_usd == pytest.approx(2.5)
    assert summary.totals.actual_cost_usd == pytest.approx(2.5)


def test_delta_tokens_absorbed_only_by_later_covering_snapshot():
    # Record A reports a 100-token delta for session "abc"; record B's later
    # cumulative snapshot of 150 provably contains it — the flow total is 150,
    # not the blind 250 sum. Tokens and cost share the absorption rule.
    delta = UsageRecord(
        call_id="a",
        attempt=0,
        usage_status=UsageStatus.AVAILABLE,
        provider="anthropic",
        provider_session_id="abc",
        logical_input_tokens=100,
        uncached_input_tokens=100,
        output_tokens=10,
        actual_cost_usd=0.1,
        cost_semantics=CostSemantics.EVENT_DELTA,
    )
    snapshot = UsageRecord(
        call_id="b",
        attempt=0,
        usage_status=UsageStatus.AVAILABLE,
        provider="anthropic",
        provider_session_id="abc",
        logical_input_tokens=150,
        uncached_input_tokens=150,
        output_tokens=15,
        actual_cost_usd=0.15,
        usage_semantics=UsageSemantics.PROVIDER_SESSION_CUMULATIVE,
        cost_semantics=CostSemantics.PROVIDER_SESSION_CUMULATIVE,
    )
    summary = UsageSummary.summarize([delta, snapshot], catalog=CATALOG)
    assert summary.totals.logical_input_tokens == 150
    assert summary.totals.output_tokens == 15
    assert summary.actual_cost_usd == pytest.approx(0.15)


def test_delta_tokens_after_snapshot_are_counted():
    # The snapshot is earlier than the delta tokens, so it cannot contain
    # them — the delta must count instead of being dropped.
    snapshot = UsageRecord(
        call_id="b",
        attempt=0,
        usage_status=UsageStatus.AVAILABLE,
        provider="anthropic",
        provider_session_id="abc",
        logical_input_tokens=150,
        uncached_input_tokens=150,
        output_tokens=15,
        usage_semantics=UsageSemantics.PROVIDER_SESSION_CUMULATIVE,
    )
    later_delta = UsageRecord(
        call_id="c",
        attempt=0,
        usage_status=UsageStatus.AVAILABLE,
        provider="anthropic",
        provider_session_id="abc",
        logical_input_tokens=100,
        uncached_input_tokens=100,
        output_tokens=10,
    )
    aggregate = aggregate_usage_records([snapshot, later_delta])
    assert aggregate.logical_input_tokens == 250
    assert aggregate.output_tokens == 25


def test_duplicate_records_are_not_double_counted_at_any_level():
    record = _record("c1", input_tokens=100, output_tokens=10, cost=0.001)
    step_summary = UsageSummary.summarize([record, record], catalog=CATALOG)
    assert step_summary.totals.total_tokens == 110
    assert step_summary.actual_cost_usd == pytest.approx(0.001)
    assert len(step_summary.records) == 1
    # Flow level: the same record replayed from history + live tracker.
    flow_summary = UsageSummary.summarize(
        [record, record, record], catalog=CATALOG
    )
    assert flow_summary.totals.total_tokens == 110
    assert flow_summary.actual_cost_usd == pytest.approx(0.001)
    assert len(flow_summary.records) == 1


def test_summary_round_trips_through_persisted_dict():
    records = [
        _record("c1", input_tokens=100, output_tokens=10, cost=0.001),
        _record("c2", input_tokens=1000, output_tokens=100),
        _record("c3", status=UsageStatus.UNAVAILABLE),
    ]
    summary = UsageSummary.summarize(records, catalog=CATALOG)
    restored = UsageSummary.from_dict(
        json.loads(json.dumps(summary.to_dict()))
    )
    assert restored.actual_cost_usd == summary.actual_cost_usd
    assert restored.estimated_cost_usd == summary.estimated_cost_usd
    assert restored.unknown_call_count == summary.unknown_call_count
    assert restored.totals.total_tokens == summary.totals.total_tokens
    assert restored.completeness == summary.completeness


def test_summary_from_dict_none_is_empty():
    summary = UsageSummary.from_dict(None)
    assert summary.records == []
    assert summary.actual_cost_usd is None
    assert summary.estimated_cost_usd is None


# ---------------------------------------------------------------------------
# Real schema fixtures through the call/step/flow payload aggregation
# ---------------------------------------------------------------------------


class TestFixtureAggregationThroughPayload:
    """Fixture records must survive call/step/flow aggregation intact.

    The shared parser output feeds ``build_usage_payload`` — the one backend
    the CLI history view, daemon and server consume — so subagent usage,
    single-CLI multi-turn calls and failure/retry usage must appear in the
    per-call table, the per-step summary and the flow totals exactly once.
    """

    @staticmethod
    def _claude_record(name, call_id, **metadata):
        return parse_usage_record(
            _fixture(name),
            call_id=call_id,
            runner_type="claude-code",
            provider="anthropic",
            **metadata,
        )

    @staticmethod
    def _codex_record(call_id):
        # The codex fixture speaks codex --json events; the runner converts
        # them before the unified parser ever sees the stream.
        from tianluo.codex_runner import CodexEventConverter

        converter = CodexEventConverter()
        output = []
        for line in _fixture("codex_exec.jsonl").splitlines():
            output.extend(converter.convert_line(line))
        output.extend(converter.finalize())
        return parse_usage_record(
            "\n".join(output),
            call_id=call_id,
            runner_type="codex",
            provider="openai",
        )

    def test_multi_turn_subagent_and_codex_survive_call_step_flow(self):
        from tianluo.usage import build_usage_payload

        multi = self._claude_record("claude_multi_turn.jsonl", "multi-call")
        subagent = self._claude_record("claude_subagent.jsonl", "subagent-call")
        codex = self._codex_record("codex-call")

        # The multi-turn record appears in two steps (a retry re-attachment);
        # the payload deduplicates by call/attempt identity.
        payload = build_usage_payload(
            {
                "01_implement": [multi, subagent],
                "02_self_check": [multi, codex],
            },
            CATALOG,
        )
        assert len(payload["calls"]) == 3
        by_call = {call["call_id"]: call for call in payload["calls"]}
        # Subagent usage is folded into the parent call record, not lost.
        assert by_call["subagent-call"]["logical_input_tokens"] == 120
        assert by_call["subagent-call"]["output_tokens"] == 30
        # Multi-turn deltas sum once; the session's latest cumulative cost wins.
        assert by_call["multi-call"]["logical_input_tokens"] == 250
        assert by_call["multi-call"]["output_tokens"] == 55
        assert by_call["multi-call"]["actual_cost_usd"] == pytest.approx(0.008)
        # Codex cached tokens are a subset of logical input; nothing lost.
        assert by_call["codex-call"]["logical_input_tokens"] == 300
        assert by_call["codex-call"]["cache_read_input_tokens"] == 120

        totals = payload["summary"]["totals"]
        assert totals["logical_input_tokens"] == 250 + 120 + 300
        assert totals["output_tokens"] == 55 + 30 + 60
        # Two provider sessions with actual costs: deltas add, no double-count.
        # The subagent call has tokens but no actual cost; its dated provider
        # model id (claude-opus-4-1-20250805) normalizes to the catalog row,
        # so it is estimated at claude-opus-4-1 prices — never folded into a
        # fake zero and never counted as an unknown model.
        assert payload["summary"]["actual_cost_usd"] == pytest.approx(0.008 + 0.009)
        assert payload["summary"]["unknown_model_count"] == 0
        assert payload["summary"]["estimated_cost_usd"] == pytest.approx(
            120 * 15 / 1_000_000 + 30 * 75 / 1_000_000
        )
        assert payload["completeness"] == "complete"

        # Per-step summaries reflect the same call/attempt units.
        implement = payload["steps"]["01_implement"]["summary"]
        assert implement["totals"]["logical_input_tokens"] == 250 + 120
        assert implement["actual_cost_usd"] == pytest.approx(0.008)
        assert implement["completeness"] == "complete"
        self_check = payload["steps"]["02_self_check"]["summary"]
        assert self_check["totals"]["logical_input_tokens"] == 250 + 300
        assert self_check["actual_cost_usd"] == pytest.approx(0.008 + 0.009)
        assert self_check["completeness"] == "complete"

    def test_failure_retry_usage_reaches_payload_as_partial(self):
        from tianluo.usage import build_usage_payload

        raw = _fixture("failure_and_missing_usage.jsonl")
        events = [json.loads(line) for line in raw.splitlines()]
        # One call per terminal result — exactly how LLMCaller records each
        # attempt of a failed/retried invocation.
        calls = {}
        for index, event in enumerate(events[1:], start=1):
            calls[f"attempt-{index}"] = parse_usage_record(
                event, call_id=f"attempt-{index}",
                runner_type="claude-code", provider="anthropic",
            )
        payload = build_usage_payload(
            {"01_implement": list(calls.values())}, None,
        )
        assert len(payload["calls"]) == 3
        # The missing-usage attempt is a counted unknown call, the quota-failed
        # attempt keeps its usage, and the explicit zero stays available.
        assert payload["summary"]["unknown_call_count"] == 1
        assert payload["summary"]["actual_cost_usd"] == pytest.approx(0.001)
        assert payload["summary"]["totals"]["output_tokens"] == 4
        assert payload["completeness"] == "partial"
        statuses = {call["call_id"]: call["usage_status"] for call in payload["calls"]}
        assert statuses["attempt-1"] == "available"
        assert statuses["attempt-2"] == "unavailable"
        assert statuses["attempt-3"] == "available"

    def test_goal_direct_fixture_flow_totals_match_parse(self):
        from tianluo.usage import build_usage_payload

        record = self._claude_record("claude_goal_direct.jsonl", "goal-call")
        payload = build_usage_payload(
            {"01_implement": [record]}, None, flow_records=[record],
        )
        summary = payload["summary"]
        assert summary["totals"]["logical_input_tokens"] == record.logical_input_tokens
        assert summary["totals"]["output_tokens"] == record.output_tokens
        assert summary["actual_cost_usd"] == record.actual_cost_usd
        assert summary["completeness"] == payload["completeness"] == "complete"


def test_distinct_events_sharing_container_id_are_aggregated():
    # Two terminal events under one container id are distinct measurements,
    # not replays: both contribute instead of the second being dropped.
    record = parse_usage_record(
        [
            _result({"input_tokens": 10, "output_tokens": 1}, id="same-id"),
            _result({"input_tokens": 20, "output_tokens": 2}, id="same-id"),
        ],
        call_id="container",
    )
    assert record.logical_input_tokens == 30
    assert record.output_tokens == 3
    assert record.usage_status == UsageStatus.AVAILABLE


def test_identical_replay_under_container_id_is_partial_not_silent():
    # A byte-identical re-emission under the same container id is dropped,
    # but the forced drop of a usage-bearing event must surface as partial.
    record = parse_usage_record(
        [
            _result({"input_tokens": 10, "output_tokens": 1}, id="same-id"),
            _result({"input_tokens": 10, "output_tokens": 1}, id="same-id"),
        ],
        call_id="replay",
    )
    assert record.logical_input_tokens == 10
    assert record.output_tokens == 1
    assert record.usage_status == UsageStatus.PARTIAL
    assert any("partial" in item for item in record.diagnostics)


def test_dedup_does_not_mutate_caller_owned_records():
    # UsageSummary re-runs dedup on every render over the same persisted
    # ledger. Mutating the kept record in place would make the PARTIAL
    # downgrade sticky and grow its diagnostics list without bound.
    first = UsageRecord(
        call_id="call-1",
        attempt=0,
        usage_status=UsageStatus.AVAILABLE,
        provider="anthropic",
        provider_session_id="session-mutate",
        logical_input_tokens=10,
        uncached_input_tokens=10,
        output_tokens=1,
        usage_event_ids=["e1", "e2"],
    )
    second = UsageRecord(
        call_id="call-2",
        attempt=0,
        usage_status=UsageStatus.AVAILABLE,
        provider="anthropic",
        provider_session_id="session-mutate",
        logical_input_tokens=20,
        uncached_input_tokens=20,
        output_tokens=2,
        usage_event_ids=["e2", "e3"],
    )
    records = [first, second]
    for _ in range(3):
        kept, _diagnostics = deduplicate_usage_records(records)
        assert len(kept) == 1
        # The merge downgrade lands on the returned copy...
        assert kept[0].usage_status == UsageStatus.PARTIAL
        assert len(kept[0].diagnostics) == 1
    # ...never on the caller's persisted records.
    assert first.usage_status == UsageStatus.AVAILABLE
    assert first.diagnostics == []
    assert second.usage_status == UsageStatus.AVAILABLE


def test_intersecting_event_sets_collapse_instead_of_blindly_summing():
    # {e1,e2} and {e2,e3} in one session overlap on e2: summing both records
    # would count e2 twice. The dedup collapses them to one representative
    # and marks it partial because the dropped record measured e3, which the
    # representative does not carry.
    first = UsageRecord(
        call_id="call-1",
        attempt=0,
        usage_status=UsageStatus.AVAILABLE,
        provider="anthropic",
        provider_session_id="session-overlap",
        logical_input_tokens=10,
        uncached_input_tokens=10,
        output_tokens=1,
        usage_event_ids=["e1", "e2"],
    )
    second = UsageRecord(
        call_id="call-2",
        attempt=0,
        usage_status=UsageStatus.AVAILABLE,
        provider="anthropic",
        provider_session_id="session-overlap",
        logical_input_tokens=20,
        uncached_input_tokens=20,
        output_tokens=2,
        usage_event_ids=["e2", "e3"],
    )
    unique, diagnostics = deduplicate_usage_records([first, second])
    assert len(unique) == 1
    assert any("overlap" in item for item in diagnostics)
    total = aggregate_usage_records([first, second])
    assert (total.logical_input_tokens, total.output_tokens) != (30, 6)
    assert total.usage_status == UsageStatus.PARTIAL


def test_unavailable_aggregate_does_not_supersede_usage_bearing_partial():
    # An UNAVAILABLE record sharing events with a usage-bearing record must
    # not become the representative: the measured record survives.
    measured = UsageRecord(
        call_id="call-1",
        attempt=0,
        usage_status=UsageStatus.AVAILABLE,
        provider="openai",
        provider_session_id="thread-overlap",
        logical_input_tokens=10,
        uncached_input_tokens=10,
        output_tokens=1,
        usage_event_ids=["event-a"],
    )
    unavailable = UsageRecord(
        call_id="call-2",
        attempt=0,
        usage_status=UsageStatus.UNAVAILABLE,
        provider="openai",
        provider_session_id="thread-overlap",
        usage_event_ids=["event-a", "event-b"],
    )
    total = aggregate_usage_records([measured, unavailable])
    assert total.logical_input_tokens == 10
    assert total.output_tokens == 1
    assert total.usage_status != UsageStatus.UNAVAILABLE


def test_chat_completions_cached_prompt_tokens_are_a_subset():
    """``prompt_tokens_details.cached_tokens`` is the Chat-Completions cached
    subset: it must be split out of ``prompt_tokens``, never billed as uncached
    input while the record still claims exact usage."""
    ndjson = json.dumps(
        {
            "type": "result",
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 800},
            },
        }
    )
    record = parse_usage_record(ndjson, call_id="c", provider="compat-proxy")
    assert record.logical_input_tokens == 1000
    assert record.uncached_input_tokens == 200
    assert record.cache_read_input_tokens == 800
    assert record.output_tokens == 10
    assert record.usage_status == UsageStatus.AVAILABLE


def test_parent_terminal_cost_absorbs_iteration_breakdown_costs():
    """A turn-level total already covers its per-iteration ``cost_usd`` lines.

    Tokens still come from the children (the parent carries none), but the cost
    must be billed exactly once — the parent total — instead of parent+children.
    """
    ndjson = json.dumps(
        {
            "type": "turn.completed",
            "total_cost_usd": 0.30,
            "iterations": [
                {"usage": {"input_tokens": 10, "output_tokens": 5}, "cost_usd": 0.10},
                {"usage": {"input_tokens": 20, "output_tokens": 7}, "cost_usd": 0.20},
            ],
        }
    )
    record = parse_usage_record(ndjson, call_id="c")
    assert record.actual_cost_usd == pytest.approx(0.30)
    assert record.logical_input_tokens == 30
    assert record.output_tokens == 12


def test_absorbed_iteration_costs_keep_distinct_child_identity():
    """Suppressing a child's covered cost must not collapse two children that
    differ only by cost into one event (which would drop the second's tokens)."""
    ndjson = json.dumps(
        {
            "type": "turn.completed",
            "total_cost_usd": 0.30,
            "iterations": [
                {"usage": {"input_tokens": 10, "output_tokens": 5}, "cost_usd": 0.10},
                {"usage": {"input_tokens": 10, "output_tokens": 5}, "cost_usd": 0.20},
            ],
        }
    )
    record = parse_usage_record(ndjson, call_id="c")
    assert record.actual_cost_usd == pytest.approx(0.30)
    assert record.logical_input_tokens == 20
    assert record.output_tokens == 10


def test_iteration_costs_still_count_without_a_parent_total():
    """Without a parent cost there is nothing to absorb: children bill normally."""
    ndjson = json.dumps(
        {
            "type": "turn.completed",
            "iterations": [
                {"usage": {"input_tokens": 10, "output_tokens": 5}, "cost_usd": 0.10},
                {"usage": {"input_tokens": 20, "output_tokens": 7}, "cost_usd": 0.20},
            ],
        }
    )
    record = parse_usage_record(ndjson, call_id="c")
    assert record.actual_cost_usd == pytest.approx(0.30)
    assert record.logical_input_tokens == 30


def test_subagent_child_session_usage_is_never_dropped():
    # A subagent running in its OWN provider session is billed separately: the
    # parent snapshot cannot cover it, so dropping it would delete reported
    # tokens and cost from every usage surface with no diagnostic.
    record = parse_usage_record(
        {
            "type": "result",
            "session_id": "main-sess",
            "usage": {"input_tokens": 900, "output_tokens": 300},
            "total_cost_usd": 0.009,
            "subagents": [
                {
                    "session_id": "sub-1",
                    "usage_event_id": "sub-evt-1",
                    "usage": {"input_tokens": 500, "output_tokens": 200},
                    "total_cost_usd": 0.004,
                }
            ],
        },
        call_id="subagent-session",
        provider="anthropic",
    )
    assert record.logical_input_tokens == 1400
    assert record.output_tokens == 500
    assert record.actual_cost_usd == pytest.approx(0.013)
    assert "sub-1" in record.provider_session_ids


def test_same_session_breakdown_is_absorbed_with_an_explicit_decision():
    # An iteration breakdown of the SAME session is already summarized by the
    # parent snapshot: absorbed once, and the absorption is stated.
    record = parse_usage_record(
        {
            "type": "result",
            "session_id": "main-sess",
            "usage": {"input_tokens": 900, "output_tokens": 300},
            "iterations": [
                {
                    "session_id": "main-sess",
                    "usage_event_id": "iter-1",
                    "usage": {"input_tokens": 400, "output_tokens": 100},
                }
            ],
        },
        call_id="same-session-breakdown",
        provider="anthropic",
    )
    assert record.logical_input_tokens == 900
    assert record.output_tokens == 300
    assert any("already covered" in item for item in record.diagnostics)


def test_tokens_and_cost_are_read_from_the_same_scope():
    # A compat proxy relays the provider's per-exchange message block while
    # adding its own top-level snapshot. Tokens must not come from the inner
    # delta while cost comes from the outer total.
    record = parse_usage_record(
        {
            "type": "result",
            "usage": {"input_tokens": 1000, "output_tokens": 500},
            "total_cost_usd": 0.02,
            "message": {"usage": {"input_tokens": 200, "output_tokens": 100}},
        },
        call_id="proxy-scope",
        provider="compat-proxy",
    )
    assert record.logical_input_tokens == 1000
    assert record.output_tokens == 500
    assert record.actual_cost_usd == pytest.approx(0.02)
    assert record.usage_status == UsageStatus.AVAILABLE


def test_competing_scopes_mark_the_record_partial():
    # Tokens can only be read from the event body while the cost sits in a
    # nested scope that reports its own usage: two measurement scopes, so the
    # record must not read as one complete measurement.
    record = parse_usage_record(
        {
            "type": "result",
            "usage": {"input_tokens": 1000, "output_tokens": 500},
            "message": {
                "usage": {"input_tokens": 200, "output_tokens": 100},
                "total_cost_usd": 0.02,
            },
        },
        call_id="scope-conflict",
        provider="compat-proxy",
    )
    assert record.usage_status == UsageStatus.PARTIAL
    assert any("different measurement scopes" in d for d in record.diagnostics)


def test_from_dict_derives_uncached_input_when_absent():
    # A wire-shaped payload naming the logical total and its cache categories
    # but omitting uncached must not read as "all input cached" (which prices
    # the uncached portion at zero).
    record = UsageRecord.from_dict(
        {
            "call_id": "wire",
            "attempt": 0,
            "usage_status": "available",
            "logical_input_tokens": 1000,
            "cache_read_input_tokens": 200,
            "output_tokens": 10,
        }
    )
    assert record.uncached_input_tokens == 800
    assert record.logical_input_tokens == 1000
    assert record.usage_status == UsageStatus.AVAILABLE


def test_from_dict_marks_inconsistent_input_split_partial():
    record = UsageRecord.from_dict(
        {
            "call_id": "wire-bad",
            "attempt": 0,
            "usage_status": "available",
            "logical_input_tokens": 1000,
            "uncached_input_tokens": 100,
            "cache_read_input_tokens": 200,
            "output_tokens": 10,
        }
    )
    assert record.usage_status == UsageStatus.PARTIAL
    assert any("logical input total" in d for d in record.diagnostics)


def test_record_less_legacy_tallies_are_not_collapsed_by_placeholder_id():
    # Two distinct legacy tallies share the placeholder call id, which
    # identifies nothing: collapsing them would silently delete one tally's
    # reported usage from the flow totals.
    first = legacy_usage_record({"input_tokens": 100, "output_tokens": 50,
                                 "total_cost_usd": 0.01})
    second = legacy_usage_record({"input_tokens": 200, "output_tokens": 100,
                                  "total_cost_usd": 0.02})
    unique, diagnostics = deduplicate_usage_records([first, second])
    assert len(unique) == 2
    total = aggregate_usage_records(unique)
    assert total.logical_input_tokens == 300
    assert total.output_tokens == 150
    assert total.actual_cost_usd == pytest.approx(0.03)
    assert any("no stable identity" in item for item in diagnostics)


def test_attributed_legacy_records_still_deduplicate():
    # A real source attribution IS an identity: the same adapted record
    # reaching one aggregation twice must still fold once.
    payload = {"input_tokens": 100, "output_tokens": 50, "total_cost_usd": 0.01}
    first = legacy_usage_record(payload, call_id="legacy:step-1:0")
    second = legacy_usage_record(payload, call_id="legacy:step-1:0")
    unique, _diagnostics = deduplicate_usage_records([first, second])
    assert len(unique) == 1
