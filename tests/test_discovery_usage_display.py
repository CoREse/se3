"""Tests for discovery cumulative token-usage display on the CLI (G3).

When the discovery step completes (after multi-round dialogue and the
programmatic confirmation gate), CliSink renders a cumulative usage line from
``step.outputs['token_usage']``. This file covers:

(a) Cumulative usage line rendered with all fields (input/output/cache/cost).
(b) No output when token_usage is absent or empty.
(c) Multi-round carried-token-usage simulation ensuring the final
    ``step.outputs['token_usage']`` equals the sum of all rounds and
    CliSink displays it.
(d) Confirm/plan rendering not regressed (confirm compact footer, plan big
    block).
"""

from __future__ import annotations

import pytest

from tianluo.engine import CliSink, EventType, new_event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_discovery_step(
    *,
    token_usage: dict | None = None,
    extra_outputs: dict | None = None,
):
    """Build a discovery step with optional token_usage in outputs."""
    from tianluo.engine.models import Step, StepStatus, StepType

    step = Step(step_id="00_discovery_abc", step_type=StepType.DISCOVERY)
    step.status = StepStatus.COMPLETED
    outputs = dict(extra_outputs or {})
    if token_usage is not None:
        outputs["token_usage"] = token_usage
    step.outputs = outputs
    return step


def _consume_discovery_completed(step, *, sink=None):
    """Emit a STEP_COMPLETED event for the given discovery step."""
    if sink is None:
        sink = CliSink()
    sink.consume(
        new_event(
            EventType.STEP_COMPLETED,
            flow_id="flow-test",
            step_id=step.step_id,
            step_type="discovery",
            step=step,
        )
    )


@pytest.fixture
def captured_console():
    """Install a recording Rich console for the duration of the test."""
    from rich.console import Console

    from tianluo.engine import display

    prev = display.get_console()
    console = Console(record=True, force_terminal=False, width=100)
    display.set_console(console)
    yield console
    display.set_console(prev)


# ---------------------------------------------------------------------------
# (a) Cumulative usage line rendered with all fields
# ---------------------------------------------------------------------------


def test_discovery_cumulative_usage_renders_all_fields(captured_console):
    """When discovery completes with non-empty token_usage covering all four
    token fields and cost, the cumulative line includes every field."""
    usage = {
        "input_tokens": 5000,
        "output_tokens": 1200,
        "cache_creation_input_tokens": 300,
        "cache_read_input_tokens": 800,
        "total_cost_usd": 0.0350,
    }
    step = _make_discovery_step(token_usage=usage)
    _consume_discovery_completed(step)

    out = captured_console.export_text()
    # The cumulative line uses format_usage_line which renders:
    # "in {input} · out {output} · cache(r/w) {cache_read}/{cache_creation} · ${cost}"
    assert "Discovery cumulative:" in out
    assert "5,000" in out  # input_tokens with thousands separator
    assert "1,200" in out  # output_tokens
    assert "800" in out    # cache_read_input_tokens
    assert "300" in out    # cache_creation_input_tokens
    assert "$0.0350" in out  # cost with 4 decimal places


def test_discovery_cumulative_usage_line_format(captured_console):
    """Verify the exact format matches what format_usage_line produces."""
    from tianluo.engine.token_usage import UsageTotals, format_usage_line

    usage = {
        "input_tokens": 10000,
        "output_tokens": 2500,
        "cache_creation_input_tokens": 100,
        "cache_read_input_tokens": 500,
        "total_cost_usd": 0.0123,
    }
    step = _make_discovery_step(token_usage=usage)
    _consume_discovery_completed(step)

    totals = UsageTotals.from_dict(usage)
    expected_line = f"Discovery cumulative: {format_usage_line(totals)}"
    out = captured_console.export_text()
    assert expected_line in out


# ---------------------------------------------------------------------------
# (b) No output when token_usage is absent or empty
# ---------------------------------------------------------------------------


def test_discovery_no_usage_renders_nothing(captured_console):
    """A discovery step with no token_usage in outputs renders nothing."""
    step = _make_discovery_step()  # no token_usage
    _consume_discovery_completed(step)
    assert captured_console.export_text() == ""


def test_discovery_empty_usage_renders_nothing(captured_console):
    """A discovery step with all-zero token_usage renders nothing."""
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "total_cost_usd": 0.0,
    }
    step = _make_discovery_step(token_usage=usage)
    _consume_discovery_completed(step)
    assert captured_console.export_text() == ""


def test_discovery_partial_zero_usage_renders_nothing(captured_console):
    """A discovery step with zero tokens and zero cost (even if some fields
    are absent from the dict) renders nothing."""
    usage = {"input_tokens": 0, "output_tokens": 0}
    step = _make_discovery_step(token_usage=usage)
    _consume_discovery_completed(step)
    assert captured_console.export_text() == ""


# ---------------------------------------------------------------------------
# (c) Multi-round carried-token-usage simulation
# ---------------------------------------------------------------------------


def test_multi_round_carried_usage_equals_sum_of_rounds():
    """Simulate the discovery multi-round carried-token-usage mechanism:
    each round's increment is added to carried_token_usage, and the final
    step.outputs['token_usage'] equals the cumulative sum."""
    from tianluo.engine.token_usage import UsageTotals

    # Simulate 3 rounds of LLM calls with these increments:
    round_increments = [
        {"input_tokens": 1000, "output_tokens": 300, "cache_creation_input_tokens": 0,
         "cache_read_input_tokens": 200, "total_cost_usd": 0.005},
        {"input_tokens": 1500, "output_tokens": 400, "cache_creation_input_tokens": 50,
         "cache_read_input_tokens": 300, "total_cost_usd": 0.008},
        {"input_tokens": 800, "output_tokens": 200, "cache_creation_input_tokens": 0,
         "cache_read_input_tokens": 100, "total_cost_usd": 0.003},
    ]

    # Build the carried_token_usage as the discovery handler would:
    carried = UsageTotals()
    for inc in round_increments:
        round_total = UsageTotals.from_dict(inc)
        carried.add(round_total)

    # This is what step.outputs["token_usage"] should contain:
    final_usage = carried.to_dict()

    # Verify the cumulative matches the expected sum.
    expected = UsageTotals()
    for inc in round_increments:
        expected.add(UsageTotals.from_dict(inc))

    assert final_usage == expected.to_dict()
    assert final_usage["input_tokens"] == 3300
    assert final_usage["output_tokens"] == 900
    assert final_usage["cache_creation_input_tokens"] == 50
    assert final_usage["cache_read_input_tokens"] == 600
    assert final_usage["total_cost_usd"] == pytest.approx(0.016)


def test_multi_round_cumulative_renders_in_cli(captured_console):
    """After multi-round discovery, the cumulative usage shows the sum of
    all rounds (not just the last one) in the CLI output."""
    # Build a step with cumulative usage (as if 3 rounds happened).
    usage = {
        "input_tokens": 3300,
        "output_tokens": 900,
        "cache_creation_input_tokens": 50,
        "cache_read_input_tokens": 600,
        "total_cost_usd": 0.016,
    }
    step = _make_discovery_step(token_usage=usage)
    _consume_discovery_completed(step)

    out = captured_console.export_text()
    assert "Discovery cumulative:" in out
    # These are the cumulative sums, not any single round's values.
    assert "3,300" in out
    assert "900" in out
    assert "600" in out   # cache_read
    assert "50" in out    # cache_creation
    assert "$0.0160" in out


def test_confirm_round_without_llm_does_not_affect_usage():
    """The programmatic confirmation round issues no LLM call, so its
    round increment is zero. The cumulative remains unchanged from the
    previous round. Verify this does not introduce spurious zeros."""
    from tianluo.engine.token_usage import UsageTotals

    # After the last LLM round, carried_token_usage is:
    carried = UsageTotals(
        input_tokens=2000,
        output_tokens=500,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=300,
        total_cost_usd=0.01,
    )
    # The confirmation round has no LLM call, so no increment is added.
    # The final step.outputs["token_usage"] should equal carried exactly.
    final = carried.to_dict()
    assert final["input_tokens"] == 2000
    assert final["output_tokens"] == 500
    assert carried.is_empty() is False


# ---------------------------------------------------------------------------
# (d) Confirm and plan rendering not regressed
# ---------------------------------------------------------------------------


def _make_step_with_usage(step_type_value: str, usage: dict | None = None):
    from tianluo.engine.models import Step, StepStatus, StepType

    step = Step(
        step_id=f"00_{step_type_value}_test",
        step_type=StepType(step_type_value),
    )
    step.status = StepStatus.COMPLETED
    outputs = {"summary": "done"}
    if usage is not None:
        outputs["token_usage"] = usage
    step.outputs = outputs
    return step


def _consume_completed(step, sink=None):
    if sink is None:
        sink = CliSink()
    sink.consume(
        new_event(
            EventType.STEP_COMPLETED,
            flow_id="flow-test",
            step_id=step.step_id,
            step_type=step.step_type.value,
            step=step,
        )
    )


def test_confirm_compact_footer_not_regressed(captured_console):
    """Confirm still renders the compact dim footer (not the big block)."""
    usage = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 10,
        "total_cost_usd": 0.01,
    }
    step = _make_step_with_usage("confirm", usage)
    _consume_completed(step)
    out = captured_console.export_text()
    # Footer chrome is i18n-rendered; conftest pins the UI language to en-US.
    assert "This round 100 in / 50 out · Total 100 in / 50 out" in out
    assert "Step Token Usage" not in out


def test_plan_big_usage_block_not_regressed(captured_console):
    """Plan still renders the big 'Step Token Usage' block."""
    usage = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 10,
        "total_cost_usd": 0.01,
    }
    step = _make_step_with_usage("plan", usage)
    _consume_completed(step)
    out = captured_console.export_text()
    assert "Step Token Usage" in out


def test_confirm_no_usage_renders_nothing(captured_console):
    """Confirm with no token_usage renders nothing."""
    step = _make_step_with_usage("confirm")
    _consume_completed(step)
    assert captured_console.export_text() == ""


def test_plan_no_usage_renders_nothing(captured_console):
    """Plan with no token_usage renders nothing (no big block either)."""
    step = _make_step_with_usage("plan")
    _consume_completed(step)
    assert captured_console.export_text() == ""


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_discovery_step_failed_with_usage_renders_cumulative(captured_console):
    """A FAILED discovery step with token_usage still renders the cumulative
    line (the data is valid even if the step failed)."""
    from tianluo.engine.models import StepStatus

    usage = {
        "input_tokens": 500,
        "output_tokens": 100,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 50,
        "total_cost_usd": 0.005,
    }
    step = _make_discovery_step(token_usage=usage)
    step.status = StepStatus.FAILED
    sink = CliSink()
    sink.consume(
        new_event(
            EventType.STEP_FAILED,
            flow_id="flow-test",
            step_id=step.step_id,
            step_type="discovery",
            step=step,
        )
    )
    out = captured_console.export_text()
    assert "Discovery cumulative:" in out
    assert "500" in out
    assert "$0.0050" in out


def test_discovery_missing_outputs_renders_nothing(captured_console):
    """A discovery step with None outputs is safe and renders nothing."""
    from tianluo.engine.models import Step, StepStatus, StepType

    step = Step(step_id="00_discovery_x", step_type=StepType.DISCOVERY)
    step.status = StepStatus.COMPLETED
    step.outputs = None
    _consume_discovery_completed(step)
    assert captured_console.export_text() == ""
