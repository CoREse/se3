"""Tests for the token-usage accounting module (``tianluo.engine.token_usage``).

Covers the :class:`UsageTotals` data structure (field-wise merge, fault-tolerant
serialization, formatting) and the step-scoped accumulator
(:func:`accumulate_step_usage` / :func:`add_call_usage`).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tianluo.engine.token_usage import (
    UsageTotals,
    accumulate_step_usage,
    add_call_usage,
    current_step_usage,
    format_cost,
    format_usage_line,
    use_step_usage,
)


class TestUsageTotals:
    def test_defaults_are_zero_and_empty(self):
        u = UsageTotals()
        assert u.input_tokens == 0
        assert u.output_tokens == 0
        assert u.cache_creation_input_tokens == 0
        assert u.cache_read_input_tokens == 0
        assert u.total_cost_usd == 0.0
        assert u.total_tokens == 0
        assert u.is_empty()

    def test_add_merges_field_by_field(self):
        a = UsageTotals(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=2,
            cache_read_input_tokens=3,
            total_cost_usd=0.01,
        )
        b = UsageTotals(
            input_tokens=100,
            output_tokens=50,
            cache_creation_input_tokens=20,
            cache_read_input_tokens=30,
            total_cost_usd=0.02,
        )
        ret = a.add(b)
        # add returns self for chaining
        assert ret is a
        assert a.input_tokens == 110
        assert a.output_tokens == 55
        assert a.cache_creation_input_tokens == 22
        assert a.cache_read_input_tokens == 33
        assert a.total_cost_usd == pytest.approx(0.03)
        # b is unchanged
        assert b.input_tokens == 100

    def test_add_none_is_noop(self):
        a = UsageTotals(input_tokens=7, total_cost_usd=0.5)
        a.add(None)
        assert a.input_tokens == 7
        assert a.total_cost_usd == 0.5

    def test_total_tokens_sums_all_four(self):
        u = UsageTotals(
            input_tokens=1,
            output_tokens=2,
            cache_creation_input_tokens=4,
            cache_read_input_tokens=8,
        )
        assert u.total_tokens == 15

    def test_is_empty_false_when_only_cost(self):
        u = UsageTotals(total_cost_usd=0.0001)
        assert not u.is_empty()

    def test_is_empty_false_when_only_tokens(self):
        u = UsageTotals(cache_read_input_tokens=1)
        assert not u.is_empty()

    def test_to_dict_is_json_primitive(self):
        u = UsageTotals(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=2,
            cache_read_input_tokens=3,
            total_cost_usd=0.0123,
        )
        d = u.to_dict()
        assert d == {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_creation_input_tokens": 2,
            "cache_read_input_tokens": 3,
            "total_cost_usd": 0.0123,
        }
        # all values are JSON primitives
        for v in d.values():
            assert isinstance(v, (int, float))

    def test_round_trip(self):
        u = UsageTotals(
            input_tokens=11,
            output_tokens=22,
            cache_creation_input_tokens=33,
            cache_read_input_tokens=44,
            total_cost_usd=0.55,
        )
        restored = UsageTotals.from_dict(u.to_dict())
        assert restored == u

    def test_from_dict_none(self):
        assert UsageTotals.from_dict(None) == UsageTotals()

    def test_from_dict_empty(self):
        assert UsageTotals.from_dict({}) == UsageTotals()

    def test_from_dict_missing_fields_default_zero(self):
        u = UsageTotals.from_dict({"input_tokens": 5})
        assert u.input_tokens == 5
        assert u.output_tokens == 0
        assert u.cache_creation_input_tokens == 0
        assert u.cache_read_input_tokens == 0
        assert u.total_cost_usd == 0.0

    def test_from_dict_none_values_coerce_to_zero(self):
        u = UsageTotals.from_dict(
            {
                "input_tokens": None,
                "output_tokens": None,
                "cache_creation_input_tokens": None,
                "cache_read_input_tokens": None,
                "total_cost_usd": None,
            }
        )
        assert u == UsageTotals()

    def test_from_dict_bad_values_coerce_to_zero(self):
        u = UsageTotals.from_dict(
            {"input_tokens": "abc", "total_cost_usd": "not-a-number"}
        )
        assert u.input_tokens == 0
        assert u.total_cost_usd == 0.0

    def test_from_dict_numeric_strings(self):
        u = UsageTotals.from_dict({"input_tokens": "42", "total_cost_usd": "0.5"})
        assert u.input_tokens == 42
        assert u.total_cost_usd == pytest.approx(0.5)


class TestFormatting:
    def test_format_cost(self):
        assert format_cost(0.0123) == "$0.0123"
        assert format_cost(0) == "$0.0000"
        assert format_cost(1.23456) == "$1.2346"

    def test_format_cost_none_safe(self):
        assert format_cost(None) == "$0.0000"

    def test_format_cost_bad_value_safe(self):
        assert format_cost("xyz") == "$0.0000"

    def test_format_usage_line_labels_and_units(self):
        u = UsageTotals(
            input_tokens=12345,
            output_tokens=6789,
            cache_creation_input_tokens=200,
            cache_read_input_tokens=1000,
            total_cost_usd=0.0123,
        )
        line = format_usage_line(u)
        # thousands separators + labels + cost present
        assert "in 12,345" in line
        assert "out 6,789" in line
        assert "1,000/200" in line  # cache(r/w)
        assert "$0.0123" in line

    def test_format_usage_line_none_safe(self):
        line = format_usage_line(None)
        assert "in 0" in line
        assert "$0.0000" in line

    def test_format_usage_line_empty_safe(self):
        line = format_usage_line(UsageTotals())
        assert "$0.0000" in line


class TestStepScopedAccumulator:
    def test_no_scope_current_is_none(self):
        assert current_step_usage() is None

    def test_add_call_usage_outside_scope_is_noop(self):
        # Must not raise even with no active scope.
        add_call_usage(UsageTotals(input_tokens=5))
        add_call_usage({"input_tokens": 5})
        add_call_usage(None)
        assert current_step_usage() is None

    def test_scope_accumulates_multiple_calls(self):
        with accumulate_step_usage() as step:
            assert current_step_usage() is step
            add_call_usage(UsageTotals(input_tokens=10, total_cost_usd=0.01))
            add_call_usage(UsageTotals(input_tokens=20, output_tokens=5, total_cost_usd=0.02))
        assert step.input_tokens == 30
        assert step.output_tokens == 5
        assert step.total_cost_usd == pytest.approx(0.03)

    def test_scope_accepts_dict_usage(self):
        with accumulate_step_usage() as step:
            add_call_usage({"input_tokens": 7, "output_tokens": 3})
            add_call_usage({"input_tokens": 1})
        assert step.input_tokens == 8
        assert step.output_tokens == 3

    def test_add_call_usage_ignores_none_and_garbage(self):
        with accumulate_step_usage() as step:
            add_call_usage(None)
            add_call_usage(12345)  # not a UsageTotals/Mapping
            add_call_usage("string")
            add_call_usage(UsageTotals(input_tokens=4))
        assert step.input_tokens == 4

    def test_scope_restored_after_exit(self):
        assert current_step_usage() is None
        with accumulate_step_usage():
            pass
        assert current_step_usage() is None

    def test_nested_scopes_isolated(self):
        with accumulate_step_usage() as outer:
            add_call_usage(UsageTotals(input_tokens=1))
            with accumulate_step_usage() as inner:
                add_call_usage(UsageTotals(input_tokens=100))
                assert current_step_usage() is inner
            # inner's usage does not leak into outer
            assert current_step_usage() is outer
            add_call_usage(UsageTotals(input_tokens=10))
        assert inner.input_tokens == 100
        assert outer.input_tokens == 11

    def test_scope_restored_on_exception(self):
        with pytest.raises(ValueError):
            with accumulate_step_usage():
                raise ValueError("boom")
        assert current_step_usage() is None


class TestUseStepUsageCrossThread:
    """Covers ``use_step_usage`` re-binding a parent accumulator across a thread
    boundary — the fix for the DAG-parallel implement path silently dropping
    every task group's token/cost usage (worker threads start with a fresh
    contextvars context that cannot see the main-thread step scope)."""

    def test_use_step_usage_none_is_noop(self):
        assert current_step_usage() is None
        with use_step_usage(None):
            assert current_step_usage() is None
            # add_call_usage stays a no-op with no scope bound.
            add_call_usage(UsageTotals(input_tokens=5))
        assert current_step_usage() is None

    def test_use_step_usage_binds_existing_accumulator(self):
        acc = UsageTotals(input_tokens=1)
        with use_step_usage(acc):
            assert current_step_usage() is acc
            add_call_usage(UsageTotals(input_tokens=10, total_cost_usd=0.02))
        assert current_step_usage() is None
        assert acc.input_tokens == 11
        assert acc.total_cost_usd == pytest.approx(0.02)

    def test_worker_thread_without_rebind_does_not_see_scope(self):
        """Reproduces the bug: a bare worker thread sees no step scope, so its
        add_call_usage is a no-op (the group's usage would be discarded)."""

        def worker():
            # Fresh thread context: no scope visible despite the parent's scope.
            assert current_step_usage() is None
            add_call_usage(UsageTotals(input_tokens=999))

        with accumulate_step_usage() as step:
            with ThreadPoolExecutor(max_workers=1) as ex:
                ex.submit(worker).result()
        # The worker's usage was dropped — the scope did not cross the boundary.
        assert step.input_tokens == 0

    def test_worker_thread_with_rebind_folds_into_parent(self):
        """The fix: the scheduling thread captures the accumulator and each
        worker re-binds it via use_step_usage, so group usage folds in."""

        with accumulate_step_usage() as step:
            captured = current_step_usage()

            def worker(n):
                with use_step_usage(captured):
                    add_call_usage(UsageTotals(input_tokens=n, total_cost_usd=0.01))

            with ThreadPoolExecutor(max_workers=4) as ex:
                futures = [ex.submit(worker, n) for n in (10, 20, 30)]
                for f in futures:
                    f.result()
        assert step.input_tokens == 60
        assert step.total_cost_usd == pytest.approx(0.03)

    def test_concurrent_folds_are_not_lost(self):
        """Many threads folding into one shared accumulator must not lose
        updates — add_call_usage serializes the read-modify-write fold."""

        n_threads = 16
        per_thread = 50
        with accumulate_step_usage() as step:
            captured = current_step_usage()
            barrier = threading.Barrier(n_threads)

            def worker():
                with use_step_usage(captured):
                    barrier.wait()  # maximize contention
                    for _ in range(per_thread):
                        add_call_usage(UsageTotals(input_tokens=1, output_tokens=2))

            with ThreadPoolExecutor(max_workers=n_threads) as ex:
                futures = [ex.submit(worker) for _ in range(n_threads)]
                for f in futures:
                    f.result()
        assert step.input_tokens == n_threads * per_thread
        assert step.output_tokens == n_threads * per_thread * 2
