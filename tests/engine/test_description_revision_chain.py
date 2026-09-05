"""The effective-description chain and its newest layer, the revision chain.

Decision 6: a dialog's ``revised_description`` is a COVERING layer, not an
appended note. What check steps accept against is therefore the revised text,
and the superseded requirement is gone from the effective description rather
than sitting beneath an ``## Additional Instructions`` heading.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tianluo.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)
from tianluo.engine.state_machine import (
    DESCRIPTION_REVISIONS_KEY,
    _compose_effective_task_description,
    _effective_task_description_base,
    latest_description_revision,
    record_description_revision,
)


def _flow(task="original task"):
    return FlowInstance(
        flow_id="d-1",
        task_description=task,
        task_type="feature",
        status=FlowStatus.RUNNING,
    )


def _add_step(flow, step_type, outputs, *, completed_at=None, status=StepStatus.COMPLETED):
    step = Step(
        step_id=f"{len(flow.state.step_history) + 1:02d}_{step_type.value}_x",
        step_type=step_type,
        status=status,
        outputs=outputs,
    )
    step.completed_at = completed_at or datetime.now()
    flow.state.add_step(step)
    return step


def _rule(flow, text, *, completed_at=None):
    """An ADJUDICATE ruling stamped through the real ordinal allocation path."""
    from tianluo.engine.state_machine import (
        DESCRIPTION_LAYER_SEQ_OUTPUT_KEY,
        next_description_layer_seq,
    )

    return _add_step(
        flow,
        StepType.ADJUDICATE,
        {
            "adjudicated_description": text,
            DESCRIPTION_LAYER_SEQ_OUTPUT_KEY: next_description_layer_seq(flow),
        },
        completed_at=completed_at,
    )


class TestRevisionChain:
    def test_original_description_is_the_default_base(self):
        assert _effective_task_description_base(_flow()) == "original task"

    def test_a_revision_covers_the_original(self):
        flow = _flow()
        record_description_revision(flow, "the corrected task", step_id="01_x")
        assert _effective_task_description_base(flow) == "the corrected task"

    def test_a_revision_covers_a_discovery_refinement(self):
        flow = _flow()
        _add_step(flow, StepType.DISCOVERY, {"refined_description": "refined task"})
        assert _effective_task_description_base(flow) == "refined task"
        record_description_revision(flow, "the corrected task")
        assert _effective_task_description_base(flow) == "the corrected task"

    def test_the_newest_revision_wins(self):
        flow = _flow()
        record_description_revision(flow, "first correction")
        record_description_revision(flow, "second correction")
        assert _effective_task_description_base(flow) == "second correction"

    def test_the_chain_records_provenance(self):
        flow = _flow()
        record_description_revision(flow, "text", step_id="02_implement_x")
        entry = flow.state.context[DESCRIPTION_REVISIONS_KEY][0]
        assert entry["text"] == "text"
        assert entry["step_id"] == "02_implement_x"
        assert entry["source"] == "interjection_dialog"
        assert entry["timestamp"]

    def test_empty_entries_are_ignored_by_the_lookup(self):
        flow = _flow()
        flow.state.context[DESCRIPTION_REVISIONS_KEY] = [
            {"text": "real"}, {"text": ""},
        ]
        assert latest_description_revision(flow)["text"] == "real"

    def test_a_malformed_chain_degrades_to_the_original(self):
        flow = _flow()
        flow.state.context[DESCRIPTION_REVISIONS_KEY] = "not a list"
        assert _effective_task_description_base(flow) == "original task"


class TestRevisionVersusAdjudication:
    def test_a_later_revision_outranks_an_earlier_ruling(self):
        """Both are covering rewrites; whichever was written later saw the other."""
        flow = _flow()
        _rule(flow, "the ruling")
        record_description_revision(flow, "the later correction")
        assert _effective_task_description_base(flow) == "the later correction"

    def test_a_later_ruling_outranks_an_earlier_revision(self):
        flow = _flow()
        record_description_revision(flow, "the earlier correction")
        _rule(flow, "the later ruling")
        assert _effective_task_description_base(flow) == "the later ruling"

    def test_an_unparseable_legacy_timestamp_yields_to_the_ruling(self):
        """Both layers predate the counter, so the wall-clock fallback runs and
        an unreadable revision timestamp cannot claim to be the newer layer."""
        flow = _flow()
        flow.state.context[DESCRIPTION_REVISIONS_KEY] = [
            {"text": "correction", "step_id": "01_x", "timestamp": "garbage"}
        ]
        _add_step(flow, StepType.ADJUDICATE, {"adjudicated_description": "ruling"})
        assert _effective_task_description_base(flow) == "ruling"

    def test_only_completed_rulings_count(self):
        flow = _flow()
        record_description_revision(flow, "correction")
        _add_step(
            flow, StepType.ADJUDICATE, {"adjudicated_description": "unfinished"},
            status=StepStatus.RUNNING,
        )
        assert _effective_task_description_base(flow) == "correction"


class TestLegacyInterjectionCompatibility:
    def test_existing_user_interjections_still_compose(self):
        """Old flows resumed under the new engine keep their old semantics."""
        flow = _flow()
        flow.state.context["user_interjections"] = [
            {"text": "legacy instruction", "step_id": "01_x"}
        ]
        composed = _compose_effective_task_description(flow)
        assert "original task" in composed
        assert "legacy instruction" in composed

    def test_a_revision_replaces_the_base_that_interjections_decorate(self):
        flow = _flow()
        flow.state.context["user_interjections"] = [{"text": "legacy instruction"}]
        record_description_revision(flow, "the corrected task")
        composed = _compose_effective_task_description(flow)
        assert composed.startswith("the corrected task")
        assert "original task" not in composed
        # The pre-existing interjection is still honoured (read-only compat).
        assert "legacy instruction" in composed

    def test_a_new_revision_writes_no_interjection_entry(self):
        flow = _flow()
        record_description_revision(flow, "the corrected task")
        assert "user_interjections" not in flow.state.context
        assert "## Additional Instructions" not in _compose_effective_task_description(
            flow
        )


class TestLayerOrderingIsClockIndependent:
    """WHY a persisted ordinal rather than ``datetime.now()``: the two covering
    layers can be written by processes on different machines (a flow paused on
    one host, resumed on another), and unsynchronised wall clocks do not order
    them. A ruling written on a fast clock would otherwise outrank a correction
    the user made afterwards, and every later check step would keep verifying
    the requirement the user had just replaced."""

    def test_the_counter_is_monotonic_across_both_layer_kinds(self):
        from tianluo.engine.state_machine import next_description_layer_seq

        flow = _flow()
        record_description_revision(flow, "a")
        ruling = _rule(flow, "b")
        record_description_revision(flow, "c")
        seqs = [
            flow.state.context[DESCRIPTION_REVISIONS_KEY][0]["seq"],
            ruling.outputs["description_layer_seq"],
            flow.state.context[DESCRIPTION_REVISIONS_KEY][1]["seq"],
        ]
        assert seqs == sorted(seqs) and len(set(seqs)) == 3
        assert next_description_layer_seq(flow) > seqs[-1]

    def test_a_later_revision_wins_despite_a_future_dated_ruling(self):
        """The ADJUDICATE host's clock runs ahead; the revision is still later."""
        flow = _flow()
        ruling = _rule(flow, "the ruling")
        ruling.completed_at = datetime.now() + timedelta(hours=6)
        record_description_revision(flow, "the later correction")
        assert _effective_task_description_base(flow) == "the later correction"

    def test_a_later_ruling_wins_despite_a_future_dated_revision(self):
        flow = _flow()
        record_description_revision(flow, "the earlier correction")
        flow.state.context[DESCRIPTION_REVISIONS_KEY][0]["timestamp"] = (
            (datetime.now() + timedelta(hours=6)).isoformat()
        )
        _rule(flow, "the later ruling")
        assert _effective_task_description_base(flow) == "the later ruling"

    def test_a_pre_counter_ruling_loses_to_any_ordinal_bearing_revision(self):
        """A missing ordinal means "written before the counter existed".

        The ruling here was produced on a host whose clock ran 6 hours fast, so
        the wall-clock comparison would hand it the win over the correction the
        user actually made afterwards. It carries no ordinal, the revision does,
        and that alone settles the order — no clock is consulted.
        """
        flow = _flow()
        _add_step(
            flow, StepType.ADJUDICATE,
            {"adjudicated_description": "the ruling"},
            completed_at=datetime.now() + timedelta(hours=6),
        )
        record_description_revision(flow, "the later correction")
        assert _effective_task_description_base(flow) == "the later correction"

    def test_a_pre_counter_revision_loses_to_any_ordinal_bearing_ruling(self):
        """The mirror case: the legacy layer is the revision this time."""
        flow = _flow()
        flow.state.context[DESCRIPTION_REVISIONS_KEY] = [
            {
                "text": "the pre-upgrade correction",
                "step_id": "01_x",
                "timestamp": (datetime.now() + timedelta(hours=6)).isoformat(),
                "source": "interjection_dialog",
            }
        ]
        _rule(flow, "the later ruling")
        assert _effective_task_description_base(flow) == "the later ruling"

    def test_two_pre_counter_layers_still_order_by_wall_clock(self):
        """Both records predate the counter, so the old comparison is all
        that is left — and it orders single-machine legacy flows correctly."""
        flow = _flow()
        _add_step(
            flow, StepType.ADJUDICATE,
            {"adjudicated_description": "the ruling"},
            completed_at=datetime.now() - timedelta(minutes=10),
        )
        flow.state.context[DESCRIPTION_REVISIONS_KEY] = [
            {
                "text": "the later correction",
                "step_id": "01_x",
                "timestamp": datetime.now().isoformat(),
                "source": "interjection_dialog",
            }
        ]
        assert _effective_task_description_base(flow) == "the later correction"

    def test_a_rerun_ruling_outranks_the_revision_it_saw(self):
        flow = _flow()
        ruling = _rule(flow, "first ruling")
        record_description_revision(flow, "the correction")
        assert _effective_task_description_base(flow) == "the correction"
        # The step re-runs in place and re-stamps: the rerun IS the later layer.
        from tianluo.engine.state_machine import next_description_layer_seq

        ruling.outputs["adjudicated_description"] = "second ruling"
        ruling.outputs["description_layer_seq"] = next_description_layer_seq(flow)
        assert _effective_task_description_base(flow) == "second ruling"

    def test_a_rewind_never_rolls_the_counter_back(self):
        from tianluo.engine.rewind import FLOW_LEVEL_CONTEXT_KEYS
        from tianluo.engine.state_machine import DESCRIPTION_LAYER_SEQ_KEY

        assert DESCRIPTION_LAYER_SEQ_KEY in FLOW_LEVEL_CONTEXT_KEYS

    def test_a_real_ruling_allocates_its_ordinal(self):
        """The handler's write path — not just the test helper — stamps it."""
        from tianluo.engine.models import Step
        from tianluo.engine.steps import adjudicate as adjmod

        flow = _flow()
        step = Step(
            step_id="01_adjudicate_x",
            step_type=StepType.ADJUDICATE,
            status=StepStatus.RUNNING,
        )
        adjmod._apply_ruling(
            step,
            {},
            {"positions": []},
            {
                "contradiction_type": "spec_internal",
                "adjudicated_description": "the ruling",
                "adjudication_rationale": "because",
                "candidate_verdicts": [],
                "covered_surfaces": [],
            },
            [],
            "original task",
            [],
            flow=flow,
        )
        assert step.outputs["description_layer_seq"] == 1
