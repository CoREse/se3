"""Cross-surface compatibility tests for plan-mode, scope and usage projections.

The same three projections — the PLAN decomposition view, the SELF_CHECK scope
audit and the usage/cost summary — must surface identically through the CLI
history view, the daemon status snapshot / session metadata / spawn protocol,
and the server API.  These tests pin the shared backends (strategy_view.py,
usage.py) and the relay surfaces that consume them.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from tianluo.daemon import protocol
from tianluo.daemon.aggregator import DaemonAggregator, FlowSnapshot
from tianluo.daemon.history import DaemonHistoryReader, SessionMeta
from tianluo.strategy_view import (
    NO_PLAN_SURFACE_REASON,
    plan_mode_view,
    scope_view,
)
from tianluo.usage import (
    UsageRecord,
    UsageStatus,
    build_usage_payload,
    legacy_usage_record,
)


def _record(
    call_id="c1",
    attempt=0,
    *,
    input_tokens=1000,
    output_tokens=100,
    cache_read=200,
    cache_create=50,
    cost=0.03,
    model="claude-opus-5",
    status=UsageStatus.AVAILABLE,
) -> UsageRecord:
    return UsageRecord(
        call_id=call_id,
        attempt=attempt,
        usage_status=status,
        agent_name="claude",
        runner_type="claude-code",
        provider="anthropic",
        resolved_model=model,
        logical_input_tokens=input_tokens,
        uncached_input_tokens=input_tokens - cache_read - cache_create,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_create,
        actual_cost_usd=cost,
    )


# --------------------------------------------------------------------------
# plan_mode_view — the shared projection
# --------------------------------------------------------------------------


class TestPlanModeView:
    def test_persisted_context_wins(self):
        view = plan_mode_view(
            {
                "plan_decomposition": "capability",
                "plan_granularity": "conservative",
                "plan_mode_reason": "selected by explicit request",
            },
            task_type="feature",
            selected_steps=["analyze", "plan", "implement"],
            plan_group_count=3,
        )
        assert view == {
            "decomposition": "capability",
            "granularity": "conservative",
            "group_count": 3,
            "reason": "selected by explicit request",
            # A persisted reason is flow data recorded at decision time, so it
            # carries no projection-authored i18n key.
            "reason_key": "",
            "legacy_strategy": None,
            "inferred": False,
        }

    def test_group_count_falls_back_to_the_context_key(self):
        # A hot/cold flow externalizes its PLAN outputs, so the count can only
        # come from the context — never from opening the cold step body.
        view = plan_mode_view(
            {
                "plan_decomposition": "capability",
                "plan_granularity": "auto",
                "plan_group_count": 2,
            },
            task_type="feature",
            selected_steps=["plan", "implement"],
        )
        assert view["group_count"] == 2

    def test_group_count_is_none_before_plan_runs(self):
        view = plan_mode_view(
            {"plan_decomposition": "capability", "plan_granularity": "auto"},
            task_type="feature",
            selected_steps=["analyze", "plan", "implement"],
        )
        assert view["group_count"] is None

    def test_missing_granularity_defaults_to_auto(self):
        view = plan_mode_view(
            {"plan_decomposition": "granular"},
            task_type="feature",
            selected_steps=["plan", "implement"],
        )
        assert view["granularity"] == "auto"

    def test_persisted_legacy_strategy_is_projected_as_legacy(self):
        # A flow created under the retired axis never made a decomposition
        # decision; the projection describes what it recorded instead of
        # fabricating a doctrine it never had.
        view = plan_mode_view(
            {"effective_implementation_strategy": "direct"},
            task_type="feature",
            selected_steps=["analyze", "implement"],
        )
        assert view["legacy_strategy"] == "direct"
        assert view["decomposition"] is None
        assert view["granularity"] is None
        assert view["inferred"] is True
        assert view["reason_key"] == "legacy_strategy"

    def test_invalid_persisted_values_fall_back_to_inference(self):
        # A corrupt context must not crash the projection; the legacy
        # selected_steps remain the only authority.
        view = plan_mode_view(
            {"plan_decomposition": "bogus"},
            task_type="bugfix",
            selected_steps=["plan", "implement"],
        )
        assert view["decomposition"] is None
        assert view["legacy_strategy"] == "planned"
        assert view["inferred"] is True

    def test_legacy_planned_inferred_from_steps(self):
        view = plan_mode_view(
            {}, task_type="bugfix", selected_steps=["plan", "implement", "self_check"],
        )
        assert view["legacy_strategy"] == "planned"
        assert view["inferred"] is True

    def test_legacy_direct_inferred_from_steps(self):
        # A choice-surface flow with IMPLEMENT but no PLAN predates the
        # plan-mode fields yet runs direct-shaped steps.
        view = plan_mode_view(
            {}, task_type="feature",
            selected_steps=["analyze", "investigate", "implement"],
        )
        assert view["legacy_strategy"] == "direct"
        assert view["inferred"] is True

    def test_no_surface_task_types_are_not_applicable(self):
        for task_type in ("small", "review", "survey"):
            view = plan_mode_view(
                {}, task_type=task_type, selected_steps=["analyze", "implement", "test"],
            )
            assert view["legacy_strategy"] == "not_applicable", task_type
            assert view["inferred"] is True

    def test_no_surface_type_ignores_an_initialized_context(self):
        # create_flow initializes the plan-mode context for EVERY flow (the type
        # is only decided by ANALYZE), so a review flow carries capability/auto
        # while its sequence contains no PLAN step at all. Projecting those
        # values would advertise a doctrine that can never run.
        for task_type in ("small", "review", "survey"):
            view = plan_mode_view(
                {
                    "plan_decomposition": "capability",
                    "plan_granularity": "auto",
                    "plan_mode_reason": "left at the project default",
                },
                task_type=task_type,
                selected_steps=["analyze", "invariant_check", "summarize"],
            )
            assert view["decomposition"] is None, task_type
            assert view["granularity"] is None, task_type
            assert view["legacy_strategy"] == "not_applicable", task_type
            # Its own sentence: "not applicable" is the answer in BOTH models,
            # so it must not be described as a retired-model record.
            assert view["reason_key"] == "no_plan_surface", task_type
            assert view["reason"] == NO_PLAN_SURFACE_REASON, task_type

    def test_no_surface_gate_does_not_touch_a_planning_type(self):
        view = plan_mode_view(
            {"plan_decomposition": "capability", "plan_granularity": "single"},
            task_type="feature",
            selected_steps=["analyze", "plan", "implement"],
        )
        assert view["decomposition"] == "capability"
        assert view["legacy_strategy"] is None

    def test_empty_state_is_unknown(self):
        view = plan_mode_view({}, task_type="", selected_steps=[])
        assert view["decomposition"] is None
        assert view["legacy_strategy"] is None
        assert view["inferred"] is False

    def test_module_never_imports_the_engine(self):
        # The daemon parses raw engine.json dicts; importing the engine here
        # would drag the whole flow package into the control plane.
        import tianluo.strategy_view as module

        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        assert "import tianluo.engine" not in source
        assert "from .engine" not in source
        assert "from tianluo.engine" not in source


# WHY these no longer assert an engine-side mirror: the engine's legacy
# inference existed because the retired strategy axis REWROTE the step
# sequence, so an old flow's path had to be read back out of its recorded
# steps. The single-path model rewrites nothing, so the engine has no such
# inference to agree with — ``plan_mode_view`` is now the sole surface that
# describes a pre-model flow, and these pin it on its own.
def test_retired_task_type_infers_direct_on_the_control_plane():
    # The unknown-type fallback is the feature sequence, so a retired task type
    # persisted in an old flow DOES have a recorded PLAN -> IMPLEMENT surface;
    # a step list without PLAN therefore describes the old direct path.
    view = plan_mode_view(
        {}, task_type="refactor", selected_steps=["analyze", "implement"],
    )
    assert view["legacy_strategy"] == "direct"


@pytest.mark.parametrize("task_type", ["feature", "bugfix", "refactor", ""])
def test_empty_selected_steps_is_unknown_on_the_control_plane(task_type):
    # Nothing on disk to infer from: the projection may not fabricate a path.
    view = plan_mode_view({}, task_type=task_type, selected_steps=[])
    assert view["decomposition"] is None
    assert view["legacy_strategy"] is None
    assert view["inferred"] is False


@pytest.mark.parametrize("task_type", ["small", "review", "survey"])
def test_empty_selected_steps_planless_type_reads_not_applicable(task_type):
    view = plan_mode_view({}, task_type=task_type, selected_steps=[])
    assert view["legacy_strategy"] == "not_applicable"


class TestScopeView:
    def test_absent_context_returns_none(self):
        assert scope_view({}) is None
        assert scope_view({"self_check_review": "not a dict"}) is None

    def test_active_round_projected(self):
        view = scope_view(
            {
                "self_check_review": {
                    "active_round": {
                        "round_id": "scr-x",
                        "scope_mode": "incremental",
                        "baseline_id": "fix-1-x",
                        "fix_iteration": 2,
                        "pass_index": 1,
                    },
                    "completed_full_rounds": 1,
                }
            }
        )
        assert view is not None
        assert view["active_round"]["scope_mode"] == "incremental"
        assert view["completed_full_rounds"] == 1

    def test_only_counts_returns_none(self):
        # No round ever ran: nothing to audit, nothing to show.
        assert scope_view({"self_check_review": {"completed_full_rounds": 0}}) is None


# --------------------------------------------------------------------------
# build_usage_payload — the shared usage/cost backend
# --------------------------------------------------------------------------


class TestUsagePayload:
    def test_calls_steps_and_summary_shape(self):
        payload = build_usage_payload(
            {"01_implement": [_record()], "02_self_check": [_record("c2", 0, cost=0.01)]},
            None,
        )
        assert payload["completeness"] == "complete"
        assert len(payload["calls"]) == 2
        assert set(payload["steps"]) == {"01_implement", "02_self_check"}
        summary = payload["summary"]
        assert summary["totals"]["logical_input_tokens"] == 2000
        assert summary["actual_cost_usd"] == pytest.approx(0.04)
        assert summary["completeness"] == "complete"
        # Records-free wire summary keeps the totals for faithful round-trips.
        assert "totals" in summary

    def test_flow_records_override_step_union(self):
        # A session accumulator is authoritative even when per-step outputs
        # disagree (they are subsets); the union must not double-count.
        payload = build_usage_payload(
            {"01_implement": [_record()]},
            None,
            flow_records=[_record("session", 0)],
        )
        assert len(payload["calls"]) == 1
        assert payload["calls"][0]["call_id"] == "session"

    def test_no_usage_is_none_not_zero(self):
        payload = build_usage_payload({}, None)
        assert payload["completeness"] == "none"
        assert payload["summary"] is None
        assert payload["legacy"] is False

    def test_legacy_record_flags_payload(self):
        legacy = legacy_usage_record(
            {"input_tokens": 100, "output_tokens": 10, "total_cost_usd": 0.001},
            call_id="legacy-1",
        )
        payload = build_usage_payload({"01_analyze": [legacy]}, None)
        # Non-zero legacy tallies keep usable numbers but their legacy
        # provenance is surfaced, never presented as modern provider reports.
        assert payload["legacy"] is True
        assert payload["calls"][0]["usage_status"] == UsageStatus.AVAILABLE.value
        assert any(
            "legacy" in diagnostic.lower()
            for diagnostic in payload["calls"][0]["diagnostics"]
        )
        # A legacy zero tally is legacy_ambiguous, never fabricated zeros.
        empty = legacy_usage_record(
            {"input_tokens": 0, "output_tokens": 0, "total_cost_usd": 0.0},
            call_id="legacy-0",
        )
        assert empty.usage_status == UsageStatus.LEGACY_AMBIGUOUS

    def test_deduplication_across_steps(self):
        shared = _record()
        payload = build_usage_payload(
            {"01_implement": [shared], "02_self_check": [shared]},
            None,
        )
        # The same call/attempt record reaching two steps counts once.
        assert len(payload["calls"]) == 1


# --------------------------------------------------------------------------
# daemon surfaces share the same backends
# --------------------------------------------------------------------------


def _engine_state(context=None, records=None, legacy_totals=None, selected_steps=None):
    state = {
        "context": context or {},
        "selected_steps": selected_steps or [],
    }
    if records is not None:
        state["session_usage_records"] = [r.to_dict() for r in records]
    if legacy_totals is not None:
        state["session_token_usage"] = legacy_totals
    return state


class TestDaemonProjections:
    def test_aggregator_projection_fields_match_shared_views(self, tmp_path):
        record = _record()
        data = {
            "flow_id": "f1",
            "task_type": "feature",
            "state": _engine_state(
                context={
                    "plan_decomposition": "capability",
                    "plan_granularity": "single",
                    "plan_mode_reason": "selected by explicit request",
                },
                records=[record],
                selected_steps=["analyze", "plan", "implement"],
            ),
        }
        plan_mode, scope, usage = DaemonAggregator._projection_fields(tmp_path, data)
        assert plan_mode == plan_mode_view(
            data["state"]["context"],
            task_type="feature",
            selected_steps=["analyze", "plan", "implement"],
        )
        assert scope is None
        assert usage is not None
        assert usage["actual_cost_usd"] == pytest.approx(0.03)
        assert usage["completeness"] == "complete"
        # The wire summary is the shared UsageSummary shape the CLI emits.
        assert set(usage) >= {
            "actual_cost_usd",
            "estimated_cost_usd",
            "totals",
            "completeness",
            "unknown_call_count",
        }

    def test_externalized_context_ref_is_resolved_not_legacy_inferred(
        self, tmp_path
    ):
        # The hot/cold persistence layer pops `context` out of the engine.json
        # header and writes it to steps/<flow_id>/_context.json, leaving only a
        # context_ref — the shape every current-format flow actually has on
        # disk. Reading the inline dict alone would fall back to legacy
        # inference and describe a current flow as if it predated the model.
        from tianluo.runtime_paths import runtime_dir

        cold = runtime_dir(tmp_path) / "state" / "steps" / "f-ref"
        cold.mkdir(parents=True)
        (cold / "_context.json").write_text(
            json.dumps({
                "context": {
                    "plan_decomposition": "capability",
                    "plan_granularity": "auto",
                    "plan_mode_reason": "left at the default",
                },
                "fix_history": [],
            }),
            encoding="utf-8",
        )
        data = {
            "flow_id": "f-ref",
            "task_type": "feature",
            "state": {
                "context_ref": {"file": "_context.json", "hash": "abc"},
                "selected_steps": ["analyze", "plan", "implement"],
            },
        }
        plan_mode, _scope, _usage = DaemonAggregator._projection_fields(
            tmp_path, data,
        )
        assert plan_mode["decomposition"] == "capability"
        assert plan_mode["granularity"] == "auto"
        assert plan_mode["legacy_strategy"] is None
        assert plan_mode["inferred"] is False

        reader_plan_mode, _ = DaemonHistoryReader._state_projections(
            tmp_path, data, "f-ref",
        )
        assert reader_plan_mode == plan_mode

    def test_group_count_follows_the_externalized_plan_body(self, tmp_path):
        # A current-format flow keeps its PLAN outputs in a cold file, so the
        # header alone cannot count the groups. The projection follows the
        # recorded cold_ref — and memoizes on its content hash, so a poll loop
        # does not re-read the whole plan document every few seconds.
        from tianluo import strategy_view as sv
        from tianluo.runtime_paths import runtime_dir

        cold = runtime_dir(tmp_path) / "state" / "steps" / "f-cold"
        cold.mkdir(parents=True)
        (cold / "_context.json").write_text(
            json.dumps({
                "context": {
                    "plan_decomposition": "capability",
                    "plan_granularity": "auto",
                },
            }),
            encoding="utf-8",
        )
        plan_body = cold / "01_plan_x.json"
        plan_body.write_text(
            json.dumps({
                "outputs": {
                    "task_groups": [{"group_id": "G1"}, {"group_id": "G2"}],
                },
            }),
            encoding="utf-8",
        )
        data = {
            "flow_id": "f-cold",
            "task_type": "feature",
            "state": {
                "context_ref": {"file": "_context.json", "hash": "ctx1"},
                "selected_steps": ["plan", "implement"],
                "steps": {
                    "01_plan_x": {
                        "step_id": "01_plan_x",
                        "step_type": "plan",
                        "status": "completed",
                        "cold_ref": {"file": "01_plan_x.json", "hash": "plan-h1"},
                    },
                },
            },
        }
        sv._COLD_GROUP_COUNT_CACHE.clear()
        plan_mode, _scope, _usage = DaemonAggregator._projection_fields(tmp_path, data)
        assert plan_mode["decomposition"] == "capability"
        assert plan_mode["group_count"] == 2

        # Same hash -> served from the cache, with no second read: deleting the
        # file cannot change the answer.
        plan_body.unlink()
        again, _s, _u = DaemonAggregator._projection_fields(tmp_path, data)
        assert again["group_count"] == 2

        # A plan revision rewrites the body and the hash, so the stale count is
        # never reused.
        plan_body.write_text(
            json.dumps({"outputs": {"task_groups": [{"group_id": "G1"}]}}),
            encoding="utf-8",
        )
        data["state"]["steps"]["01_plan_x"]["cold_ref"]["hash"] = "plan-h2"
        revised, _s2, _u2 = DaemonAggregator._projection_fields(tmp_path, data)
        assert revised["group_count"] == 1

    def test_unreadable_cold_plan_body_is_unknown_not_memoized(self, tmp_path):
        # A transient read failure must not pin "unknown" for the life of the
        # plan revision — the next poll has to try again.
        from tianluo import strategy_view as sv
        from tianluo.runtime_paths import runtime_dir

        state = {
            "selected_steps": ["plan", "implement"],
            "steps": {
                "01_plan_x": {
                    "step_type": "plan",
                    "cold_ref": {"file": "01_plan_x.json", "hash": "missing-h"},
                },
            },
        }
        state_dir = runtime_dir(tmp_path) / "state"
        sv._COLD_GROUP_COUNT_CACHE.clear()
        assert sv.plan_group_count_from_state(
            state, state_dir=state_dir, flow_id="f-miss"
        ) is None
        assert "missing-h" not in sv._COLD_GROUP_COUNT_CACHE

        cold = state_dir / "steps" / "f-miss"
        cold.mkdir(parents=True)
        (cold / "01_plan_x.json").write_text(
            json.dumps({"outputs": {"plan_group_count": 3}}), encoding="utf-8"
        )
        assert sv.plan_group_count_from_state(
            state, state_dir=state_dir, flow_id="f-miss"
        ) == 3

    def test_aggregator_projection_legacy_inference(self, tmp_path):
        data = {
            "flow_id": "f2",
            "task_type": "bugfix",
            "state": _engine_state(selected_steps=["plan", "implement"]),
        }
        plan_mode, scope, usage = DaemonAggregator._projection_fields(tmp_path, data)
        assert plan_mode["legacy_strategy"] == "planned"
        assert plan_mode["decomposition"] is None
        assert plan_mode["inferred"] is True
        assert usage is None  # no records at all -> omitted, not zero

    def test_aggregator_legacy_totals_adapt(self, tmp_path):
        data = {
            "flow_id": "f3",
            "task_type": "feature",
            "state": _engine_state(
                legacy_totals={
                    "input_tokens": 500,
                    "output_tokens": 50,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "total_cost_usd": 0.01,
                },
                selected_steps=["plan", "implement"],
            ),
        }
        _plan_mode, _scope, usage = DaemonAggregator._projection_fields(tmp_path, data)
        assert usage is not None
        assert usage["totals"]["logical_input_tokens"] == 500
        # The old five-field tally is recovered as usable numbers (G6 keeps
        # non-zero adaptations AVAILABLE), so no misleading zero appears.
        assert usage["totals"]["output_tokens"] == 50

    def test_degraded_header_read_yields_none(self, tmp_path):
        plan_mode, scope, usage = DaemonAggregator._projection_fields(
            tmp_path, {"flow_id": "f4", "status": "running"}
        )
        assert plan_mode is None and scope is None and usage is None

    def test_flow_snapshot_to_dict_omits_absent_projections(self):
        snap = FlowSnapshot(project_root="/p", flow_id="f")
        data = snap.to_dict()
        assert "plan_mode" not in data
        assert "review_scope" not in data
        assert "usage_summary" not in data
        snap.plan_mode = {"decomposition": "capability", "group_count": 2}
        data = snap.to_dict()
        assert data["plan_mode"]["decomposition"] == "capability"

    def test_session_meta_carries_projections_when_recoverable(self, tmp_path):
        reader = DaemonHistoryReader(project_roots_provider=lambda: [str(tmp_path)])
        record = _record()
        data = {
            "flow_id": "f5",
            "status": "completed",
            "task_description": "t",
            "task_type": "feature",
            "created_at": "2026-08-13T00:00:00",
            "updated_at": "2026-08-13T00:00:00",
            "state": _engine_state(
                context={
                    "plan_decomposition": "granular",
                    "plan_granularity": "auto",
                    "plan_mode_reason": "selected by project configuration",
                },
                records=[record],
                selected_steps=["plan", "implement"],
            ),
        }
        meta = reader._meta_from_engine(tmp_path, data, source="archived")
        assert meta.plan_mode["decomposition"] == "granular"
        assert meta.usage_summary["totals"]["logical_input_tokens"] == 1000
        assert "plan_mode" in meta.to_dict()
        # History-only flows never guess a plan mode or usage on the index path.
        history_dir = tmp_path / "tianluo" / "history" / "f6"
        history_dir.mkdir(parents=True)
        (history_dir / "_meta.json").write_text(json.dumps({"type": "small"}))
        history_meta = reader._meta_from_history(tmp_path, history_dir)
        assert history_meta.plan_mode["legacy_strategy"] == "not_applicable"
        assert history_meta.usage_summary is None
        assert "usage_summary" not in history_meta.to_dict()


# --------------------------------------------------------------------------
# daemon history reader: usage recovery from jsonl
# --------------------------------------------------------------------------


def _write_jsonl(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )


class TestDaemonReadFlowUsage:
    def test_full_read_recovers_usage(self, tmp_path):
        flow_dir = tmp_path / "tianluo" / "history" / "f7"
        record = _record()
        _write_jsonl(
            flow_dir / "01_implement_abc.jsonl",
            [
                {
                    "role": "user",
                    "content": "prompt",
                    "raw_json": [],
                    "step_type": "implement",
                },
                {
                    "role": "assistant",
                    "content": "done",
                    "raw_json": [],
                    "step_type": "implement",
                    "usage_records": [record.to_dict()],
                },
            ],
        )
        reader = DaemonHistoryReader(project_roots_provider=lambda: [str(tmp_path)])
        read = reader.read_flow("f7", project_root=str(tmp_path))
        assert read.mode == "full"
        assert read.usage is not None
        assert read.usage["completeness"] == "complete"
        assert len(read.usage["calls"]) == 1
        assert read.usage["calls"][0]["resolved_model"] == "claude-opus-5"

    def test_append_read_omits_usage(self, tmp_path):
        flow_dir = tmp_path / "tianluo" / "history" / "f8"
        _write_jsonl(
            flow_dir / "01_analyze_abc.jsonl",
            [{"role": "user", "content": "p", "raw_json": [], "step_type": "analyze"}],
        )
        reader = DaemonHistoryReader(project_roots_provider=lambda: [str(tmp_path)])
        full = reader.read_flow("f8", project_root=str(tmp_path))
        assert full.usage is None  # no assistant usage records
        append = reader.read_flow(
            "f8", project_root=str(tmp_path), cursor=full.cursor
        )
        assert append.mode == "append"
        assert append.usage is None  # append windows never summarize alone

    def test_legacy_token_usage_adapts_with_flag(self, tmp_path):
        flow_dir = tmp_path / "tianluo" / "history" / "f9"
        _write_jsonl(
            flow_dir / "01_plan_abc.jsonl",
            [
                {
                    "role": "assistant",
                    "content": "plan",
                    "raw_json": [],
                    "step_type": "plan",
                    "token_usage": {
                        "input_tokens": 300,
                        "output_tokens": 30,
                        "cache_creation_input_tokens": 10,
                        "cache_read_input_tokens": 20,
                        "total_cost_usd": 0.005,
                    },
                }
            ],
        )
        reader = DaemonHistoryReader(project_roots_provider=lambda: [str(tmp_path)])
        read = reader.read_flow("f9", project_root=str(tmp_path))
        assert read.usage is not None
        assert read.usage["legacy"] is True
        assert read.usage["calls"][0]["usage_status"] == "available"
        assert any(
            "legacy" in diagnostic.lower()
            for diagnostic in read.usage["calls"][0]["diagnostics"]
        )


# --------------------------------------------------------------------------
# protocol: spawn plan mode + history-data usage
# --------------------------------------------------------------------------


class TestProtocolPlanModeAndUsage:
    def test_spawn_omits_plan_mode_by_default(self):
        msg = protocol.make_spawn_flow("t", project_root="/p")
        assert "plan_decomposition" not in msg.payload
        assert "plan_granularity" not in msg.payload
        assert msg.payload == {
            "task_description": "t",
            "project_root": "/p",
            "task_type": "feature",
            "discover": False,
        }

    def test_spawn_carries_valid_plan_mode(self):
        for value in ("capability", "granular"):
            msg = protocol.make_spawn_flow(
                "t", project_root="/p", plan_decomposition=value
            )
            assert msg.payload["plan_decomposition"] == value
        for value in ("auto", "single", "conservative"):
            msg = protocol.make_spawn_flow(
                "t", project_root="/p", plan_granularity=value
            )
            assert msg.payload["plan_granularity"] == value

    def test_spawn_rejects_invalid_plan_mode(self):
        with pytest.raises(protocol.ProtocolError):
            protocol.make_spawn_flow("t", project_root="/p", plan_decomposition="fast")
        with pytest.raises(protocol.ProtocolError):
            protocol.make_spawn_flow("t", project_root="/p", plan_granularity="planned")

    def test_spawn_plan_mode_supported_values_sets(self):
        assert protocol.SPAWN_PLAN_DECOMPOSITION_VALUES == frozenset(
            {"capability", "granular"}
        )
        assert protocol.SPAWN_PLAN_GRANULARITY_VALUES == frozenset(
            {"auto", "single", "conservative"}
        )

    def test_supports_spawn_plan_mode_gate(self):
        assert protocol.supports_spawn_plan_mode("8") is True
        assert protocol.supports_spawn_plan_mode("7") is False
        assert protocol.supports_spawn_plan_mode(None) is False
        assert protocol.supports_spawn_plan_mode("bogus") is False
        assert protocol.MIN_SPAWN_PLAN_MODE_PROTOCOL_VERSION == 8

    def test_retired_strategy_field_still_speakable_for_one_version(self):
        # A pre-8 server still sends it; the wire schema keeps accepting it so
        # the daemon can translate rather than drop the operator's intent.
        msg = protocol.make_spawn_flow(
            "t", project_root="/p", implementation_strategy="direct"
        )
        assert msg.payload["implementation_strategy"] == "direct"
        assert protocol.SPAWN_STRATEGY_PLAN_MODE_MAP["direct"] == (None, "single")
        assert protocol.SPAWN_STRATEGY_PLAN_MODE_MAP["planned"] == ("granular", None)
        assert protocol.SPAWN_STRATEGY_PLAN_MODE_MAP["auto"] == (None, None)

    def test_resume_spawn_never_carries_plan_mode(self):
        # The server's resume endpoint never attaches it; the constructor
        # keeps it off the wire for the persisted path.
        msg = protocol.make_spawn_flow("", resume_flow_id="f")
        assert "plan_decomposition" not in msg.payload
        assert "plan_granularity" not in msg.payload

    def test_history_data_usage_field(self):
        plain = protocol.make_history_data("f", "full", [{"step_id": "s"}])
        assert "usage" not in plain.payload
        payload = {"calls": [], "summary": None, "completeness": "none"}
        with_usage = protocol.make_history_data(
            "f", "full", [{"step_id": "s"}], usage=payload
        )
        assert with_usage.payload["usage"] == payload


# --------------------------------------------------------------------------
# server-side surfaces
# --------------------------------------------------------------------------


class TestServerBundleUsage:
    def test_bundle_usage_prefers_stored_payload(self):
        from tianluo.server.state import ServerState

        stored = {"completeness": "complete", "actual_cost_usd": 0.5}
        bundle = {"flow_id": "f", "usage": stored}
        assert ServerState._bundle_usage(bundle) == stored

    def test_bundle_usage_rebuilds_from_records(self):
        from tianluo.pricing import PricingCatalog
        from tianluo.server.state import ServerState

        record = _record()
        bundle = {
            "flow_id": "f",
            "records": [
                {
                    "step_id": "01_implement_x",
                    "step_type": "implement",
                    "ordinal": 1,
                    "message": {
                        "role": "assistant",
                        "usage_records": [record.to_dict()],
                    },
                }
            ],
        }
        usage = ServerState._bundle_usage(bundle)
        assert usage is not None
        assert usage["completeness"] == "complete"
        assert len(usage["calls"]) == 1
        # The rebuilt payload uses the SAME shared backend shape as the
        # daemon-computed one — one formula everywhere — and the built-in
        # catalog, never a catalog-less rebuild that would price nothing.
        from tianluo.usage import build_usage_payload

        direct = build_usage_payload(
            {"01_implement_x": [record]}, PricingCatalog.builtin(), call_id="f"
        )
        assert set(usage) == set(direct)
        assert usage["summary"] == direct["summary"]

    def test_bundle_usage_rebuild_without_catalog_prices_with_builtin(self):
        # No stored catalog (a version-skewed daemon): the rebuild still
        # prices with the built-in table — estimate_record_cost must never
        # see "no pricing catalog", which would flip priced estimates to
        # unknown-price and degrade completeness to partial.
        from tianluo.server.state import ServerState

        record = _record(cost=None)
        bundle = {
            "flow_id": "f",
            "records": [
                {
                    "step_id": "01_implement_x",
                    "step_type": "implement",
                    "ordinal": 1,
                    "message": {
                        "role": "assistant",
                        "usage_records": [record.to_dict()],
                    },
                }
            ],
        }
        usage = ServerState._bundle_usage(bundle)
        summary = usage["summary"]
        assert summary["estimated_cost_usd"] is not None
        assert summary["unknown_price_count"] == 0
        assert usage["completeness"] == "complete"

    def test_bundle_usage_rebuild_respects_stored_catalog(self):
        # A bundle that carries the daemon's project catalog: the rebuild
        # prices with the PROJECT's overrides, so the WebUI shows the same
        # estimate as ``luo history show`` on the owning machine.
        from tianluo.pricing import PricingCatalog
        from tianluo.server.state import ServerState

        override = PricingCatalog.builtin().with_overrides(
            {
                "claude-opus-5": {
                    "uncached_input": 1.0,
                    "output": 2.0,
                    "cache_read": 0.5,
                    "cache_creation": 0.5,
                    "cache_creation_5m": 0.5,
                    "cache_creation_1h": 0.5,
                }
            }
        )
        record = _record(
            cost=None, input_tokens=1000, output_tokens=100,
            cache_read=0, cache_create=0,
        )
        bundle = {
            "flow_id": "f",
            "usage_catalog": override.to_dict(),
            "records": [
                {
                    "step_id": "01_implement_x",
                    "step_type": "implement",
                    "ordinal": 1,
                    "message": {
                        "role": "assistant",
                        "usage_records": [record.to_dict()],
                    },
                }
            ],
        }
        usage = ServerState._bundle_usage(bundle)
        # 1000 uncached input + 100 output at USD 1.0 / 2.0 per million.
        assert usage["summary"]["estimated_cost_usd"] == pytest.approx(
            1e-3 + 2e-4
        )
        # Distinct from the built-in price for the same tokens.
        builtin = ServerState._bundle_usage(
            {
                "flow_id": "f",
                "records": bundle["records"],
            }
        )
        assert builtin["summary"]["estimated_cost_usd"] != pytest.approx(
            1e-3 + 2e-4
        )

    def test_refresh_bundle_usage_prices_with_stored_catalog(self):
        # A usage-bearing append refreshes the stored payload with the SAME
        # catalog that priced the daemon's full-frame payload — project
        # overrides included — instead of replacing it with a catalog-less
        # rebuild whose estimates flip to unknown-price between frames.
        from tianluo.pricing import PricingCatalog
        from tianluo.server.state import ServerState
        from tianluo.usage import build_usage_payload

        override = PricingCatalog.builtin().with_overrides(
            {
                "claude-opus-5": {
                    "uncached_input": 1.0,
                    "output": 2.0,
                    "cache_read": 0.5,
                    "cache_creation": 0.5,
                    "cache_creation_5m": 0.5,
                    "cache_creation_1h": 0.5,
                }
            }
        )
        old_record = _record(
            "c1", cost=None, input_tokens=500, output_tokens=50,
            cache_read=0, cache_create=0,
        )
        new_record = _record(
            "c2", cost=None, input_tokens=1000, output_tokens=100,
            cache_read=0, cache_create=0,
        )
        records = [
            {
                "step_id": "01_implement_x",
                "step_type": "implement",
                "ordinal": 0,
                "message": {
                    "role": "assistant",
                    "usage_records": [old_record.to_dict()],
                },
            },
            {
                "step_id": "01_implement_x",
                "step_type": "implement",
                "ordinal": 1,
                "message": {
                    "role": "assistant",
                    "usage_records": [new_record.to_dict()],
                },
            },
        ]
        # The daemon's full-frame payload, priced with the override catalog.
        daemon_payload = build_usage_payload(
            {"01_implement_x": [old_record]}, override, call_id="f"
        )
        bundle = {
            "flow_id": "f",
            "usage": daemon_payload,
            "usage_catalog": override.to_dict(),
            # The append branch extends the records BEFORE refreshing, so the
            # bundle already holds the new record.
            "records": records,
        }
        ServerState._refresh_bundle_usage(bundle, [records[1]])
        refreshed = bundle["usage"]
        assert refreshed is not daemon_payload
        # Both calls priced with the override: 1500 input + 150 output.
        assert refreshed["summary"]["estimated_cost_usd"] == pytest.approx(
            1.5e-3 + 3e-4
        )
        assert refreshed["summary"]["unknown_price_count"] == 0
        assert refreshed["completeness"] == "complete"

    def test_apply_history_frame_append_stores_catalog_and_refreshes(self):
        # The end-to-end append path: a usage-bearing append whose frame
        # carries the daemon's project catalog must leave the stored payload
        # priced with that catalog (the WebUI then agrees with the CLI).
        import asyncio

        from tianluo.pricing import PricingCatalog
        from tianluo.server.state import ServerState

        override = PricingCatalog.builtin().with_overrides(
            {
                "claude-opus-5": {
                    "uncached_input": 1.0,
                    "output": 2.0,
                    "cache_read": 0.5,
                    "cache_creation": 0.5,
                    "cache_creation_5m": 0.5,
                    "cache_creation_1h": 0.5,
                }
            }
        )
        step_file = "01_implement_x.jsonl"

        def history_record(ordinal, call_id):
            record = _record(
                call_id, cost=None, input_tokens=1000, output_tokens=100,
                cache_read=0, cache_create=0,
            )
            return {
                "step_id": "01_implement_x",
                "step_type": "implement",
                "ordinal": ordinal,
                "message": {
                    "role": "assistant",
                    "usage_records": [record.to_dict()],
                },
            }

        async def scenario():
            state = ServerState()
            await state.apply_history_frame(
                "f",
                protocol.HISTORY_MODE_FULL,
                [history_record(0, "c1")],
                cursor={step_file: 1},
                machine_id="m1",
                usage_catalog=override.to_dict(),
            )
            await state.apply_history_frame(
                "f",
                protocol.HISTORY_MODE_APPEND,
                [history_record(1, "c2")],
                cursor={step_file: 2},
                cursor_base={step_file: 1},
                machine_id="m1",
                usage_catalog=override.to_dict(),
            )
            usage = await state.get_history_usage("f")
            assert usage is not None
            assert usage["summary"]["estimated_cost_usd"] == pytest.approx(
                2e-3 + 4e-4  # two calls at the override price
            )
            assert usage["summary"]["unknown_price_count"] == 0
            assert usage["completeness"] == "complete"

        asyncio.run(scenario())

    def test_bundle_usage_legacy_records_flagged(self):
        from tianluo.server.state import ServerState

        bundle = {
            "flow_id": "f",
            "records": [
                {
                    "step_id": "01_analyze_x",
                    "step_type": "analyze",
                    "ordinal": 0,
                    "message": {
                        "role": "assistant",
                        "token_usage": {
                            "input_tokens": 100,
                            "output_tokens": 10,
                            "total_cost_usd": 0.001,
                        },
                    },
                }
            ],
        }
        usage = ServerState._bundle_usage(bundle)
        assert usage["legacy"] is True
        assert usage["calls"][0]["usage_status"] == "available"
        assert any(
            "legacy" in diagnostic.lower()
            for diagnostic in usage["calls"][0]["diagnostics"]
        )

    def test_bundle_usage_none_for_empty_bundle(self):
        from tianluo.server.state import ServerState

        assert ServerState._bundle_usage({"flow_id": "f", "records": []}) is None
        assert ServerState._bundle_usage({"flow_id": "f"}) is None

    def test_server_flow_snapshot_relays_projections(self):
        from tianluo.server.state import FlowSnapshot as ServerFlowSnapshot

        snap = ServerFlowSnapshot.from_payload(
            {
                "flow_id": "f",
                "project_root": "/p",
                "plan_mode": {"decomposition": "capability", "group_count": 1},
                "review_scope": {"active_round": {"scope_mode": "full"}},
                "usage_summary": {"actual_cost_usd": 0.25, "totals": {}},
            }
        )
        data = snap.to_dict()
        assert data["plan_mode"]["decomposition"] == "capability"
        assert data["review_scope"]["active_round"]["scope_mode"] == "full"
        assert data["usage_summary"]["actual_cost_usd"] == 0.25

    def test_server_flow_snapshot_omits_absent_projections(self):
        from tianluo.server.state import FlowSnapshot as ServerFlowSnapshot

        snap = ServerFlowSnapshot.from_payload({"flow_id": "f", "project_root": "/p"})
        data = snap.to_dict()
        assert "plan_mode" not in data
        assert "review_scope" not in data
        assert "usage_summary" not in data


class TestLegacyTaskGroupsDaemonCompat:
    """Old flows carrying task_groups / adjudicated_plan keep working.

    The daemon projections (plan mode / scope / usage) never touch the legacy
    scheduling data beyond counting the groups — they must neither crash on it
    nor leak it into the new surfaces, so an old flow's resume / display path
    is unchanged.
    """
    def _legacy_engine(self, tmp_path, flow_id="legacy-groups"):
        state_dir = tmp_path / "tianluo" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        engine = {
            "flow_id": flow_id,
            "status": "completed",
            "task_description": "legacy flow",
            "task_type": "feature",
            "created_at": "2026-08-13T00:00:00",
            "updated_at": "2026-08-13T00:00:00",
            "state": {
                "selected_steps": ["plan", "implement", "self_check"],
                "steps": {
                    "01_plan_legacy": {
                        "step_id": "01_plan_legacy",
                        "step_type": "plan",
                        "status": "completed",
                        "outputs": {
                            "task_groups": [
                                {"group_id": "G1", "tasks": [{"description": "t"}]}
                            ]
                        },
                    },
                    "02_self_check_legacy": {
                        "step_id": "02_self_check_legacy",
                        "step_type": "self_check",
                        "status": "revision_needed",
                        "outputs": {
                            "issues": [
                                {
                                    "description": "legacy issue",
                                    "expectation_source": {
                                        "type": "plan_task",
                                        "verbatim_quote": "t",
                                    },
                                }
                            ],
                            "adjudicated_plan": [{"group_id": "G1"}],
                        },
                    },
                },
            },
        }
        (state_dir / "engine.json").write_text(json.dumps(engine), encoding="utf-8")
        return engine

    def test_index_and_snapshot_tolerate_legacy_outputs(self, tmp_path):
        self._legacy_engine(tmp_path)
        reader = DaemonHistoryReader(project_roots_provider=lambda: [str(tmp_path)])
        metas = reader.build_index()
        assert metas, "legacy flow must appear in the index"
        meta = next(m for m in metas if m.flow_id == "legacy-groups")
        # The legacy steps infer the planned path; the group count is the only
        # thing read off task_groups, which never leak into the meta dict.
        assert meta.plan_mode["legacy_strategy"] == "planned"
        assert meta.plan_mode["group_count"] == 1
        assert "task_groups" not in meta.to_dict()

        aggregator = DaemonAggregator()
        aggregator.add_project_root(str(tmp_path))
        snapshot = aggregator.get_snapshot()
        flow = next(f for f in snapshot.flows if f.flow_id == "legacy-groups")
        assert flow.plan_mode["legacy_strategy"] == "planned"
        assert "task_groups" not in flow.to_dict()

    def test_degraded_legacy_header_does_not_crash(self, tmp_path):
        state_dir = tmp_path / "tianluo" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "engine.json").write_text(
            json.dumps(
                {
                    "flow_id": "giant-legacy",
                    "status": "running",
                    "task_description": "legacy",
                }
            ),
            encoding="utf-8",
        )
        reader = DaemonHistoryReader(project_roots_provider=lambda: [str(tmp_path)])
        metas = reader.build_index()
        assert any(m.flow_id == "giant-legacy" for m in metas)
        meta = next(m for m in metas if m.flow_id == "giant-legacy")
        assert meta.plan_mode is None
        assert meta.usage_summary is None


# --------------------------------------------------------------------------
# mixed legacy/modern usage ledgers
# --------------------------------------------------------------------------


class TestMixedLegacyModernUsage:
    """Modern UsageRecords and adapted legacy tallies in one ledger.

    The authoritative session accumulator and per-step records must never
    double-count the same call/attempt, and a legacy-adapted record keeps its
    provenance flag even when its non-zero numbers are usable.
    """

    def test_mixed_records_sum_once_and_flag_legacy(self):
        modern = _record(call_id="modern-call", cost=0.03)
        legacy = legacy_usage_record(
            {
                "input_tokens": 300,
                "output_tokens": 30,
                "cache_creation_input_tokens": 10,
                "cache_read_input_tokens": 20,
                "total_cost_usd": 0.005,
            },
            call_id="legacy-call",
        )
        payload = build_usage_payload({"01_implement": [modern, legacy]}, None)
        assert len(payload["calls"]) == 2
        totals = payload["summary"]["totals"]
        # 1000 modern + legacy (300 uncached + 20 read + 10 creation): the
        # legacy tally's input_tokens is Anthropic-shaped uncached input.
        assert totals["logical_input_tokens"] == 1330
        assert totals["output_tokens"] == 130
        assert totals["cache_read_input_tokens"] == 220
        assert totals["cache_creation_input_tokens"] == 60
        assert payload["legacy"] is True
        by_call = {call["call_id"]: call for call in payload["calls"]}
        assert by_call["modern-call"]["usage_status"] == "available"
        assert by_call["legacy-call"]["usage_status"] == "available"
        # Both billing units carry actual cost — but the legacy call's model
        # and provider provenance are missing, so the ledger reads partial
        # instead of a confident "complete" beside an unknown-model row.
        assert payload["completeness"] == "partial"
        assert payload["summary"]["unknown_model_count"] == 1
        assert payload["summary"]["actual_cost_usd"] == pytest.approx(0.035)

    def test_legacy_ambiguous_marks_payload_partial(self):
        modern = _record(call_id="modern-call", cost=0.03)
        legacy_zero = legacy_usage_record(
            {"input_tokens": 0, "output_tokens": 0, "total_cost_usd": 0.0},
            call_id="legacy-zero",
        )
        payload = build_usage_payload(
            {"01_implement": [modern, legacy_zero]}, None,
        )
        assert payload["legacy"] is True
        # The legacy zero tally is surfaced as an unknown call, never folded
        # into a fake-zero "complete" summary.
        assert payload["completeness"] == "partial"
        assert payload["summary"]["unknown_call_count"] == 1
        assert payload["summary"]["actual_cost_usd"] == pytest.approx(0.03)

    def test_flow_records_authoritative_over_mixed_step_union(self):
        modern = _record(call_id="modern-call")
        legacy = legacy_usage_record(
            {"input_tokens": 300, "output_tokens": 30, "total_cost_usd": 0.005},
            call_id="legacy-call",
        )
        payload = build_usage_payload(
            {
                "01_implement": [modern, legacy],
                "02_self_check": [modern],  # same modern call re-appears
            },
            None,
            flow_records=[modern, legacy],
        )
        # The session accumulator wins; neither the step union nor the
        # repeated per-step occurrence of ``modern`` double-counts it.
        assert len(payload["calls"]) == 2
        assert payload["summary"]["totals"]["logical_input_tokens"] == 1300


# --------------------------------------------------------------------------
# one engine.json -> identical CLI / daemon projections
# --------------------------------------------------------------------------


class TestSameEngineCrossSurfaceConsistency:
    """The CLI, daemon and server must project the SAME plan mode / scope /
    usage from one engine.json.

    The daemon parses the raw dict; the CLI loads the same file through the
    engine's PersistenceManager.  Both surfaces share the stdlib-only
    plan_mode_view / scope_view / UsageSummary backends, and this test pins
    that the two consumption paths still agree end to end.
    """

    def _write_engine(self, tmp_path, *, plan_context=None, scope_state=None):
        record = _record()
        now = "2026-08-13T00:00:00"
        context = dict(plan_context or {})
        if scope_state is not None:
            context["self_check_review"] = scope_state
        state = {
            "selected_steps": ["analyze", "implement", "test", "self_check"],
            "context": context,
            "session_usage_records": [record.to_dict()],
            "session_token_usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "total_cost_usd": 0.0,
            },
            "steps": {
                "01_implement_x": {
                    "step_id": "01_implement_x",
                    "step_type": "implement",
                    "status": "completed",
                    "inputs": {},
                    "outputs": {"usage_records": [record.to_dict()]},
                }
            },
            "step_history": ["01_implement_x"],
        }
        engine = {
            "flow_id": "cross-surface",
            "status": "completed",
            "task_description": "shared projection flow",
            "task_type": "feature",
            "state": state,
            "created_at": now,
            "updated_at": now,
        }
        state_dir = tmp_path / "tianluo" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "engine.json").write_text(
            json.dumps(engine), encoding="utf-8"
        )
        return engine

    def test_daemon_and_cli_projections_match(self, tmp_path):
        engine = self._write_engine(
            tmp_path,
            plan_context={
                "plan_decomposition": "capability",
                "plan_granularity": "single",
                "plan_mode_reason": "selected by explicit request",
            },
            scope_state={
                "active_round": {
                    "round_id": "scr-shared",
                    "scope_mode": "incremental",
                    "baseline_id": "fix-1-shared",
                    "fix_iteration": 2,
                    "pass_index": 1,
                },
                "completed_full_rounds": 1,
            },
        )

        daemon_plan_mode, daemon_scope, daemon_usage = (
            DaemonAggregator._projection_fields(tmp_path, engine)
        )
        assert daemon_plan_mode["decomposition"] == "capability"
        assert daemon_plan_mode["granularity"] == "single"
        assert daemon_scope["active_round"]["scope_mode"] == "incremental"
        assert daemon_usage["completeness"] == "complete"

        # CLI side: the same bytes go through the engine's loader and the
        # history command's shared-backend consumers.
        from tianluo.commands import history_cmd
        from tianluo.engine.persistence import PersistenceManager
        from tianluo.strategy_view import plan_mode_view, scope_view

        flow = PersistenceManager(tmp_path).load_flow()
        assert flow is not None
        cli_plan_mode = plan_mode_view(
            flow.state.context,
            task_type=flow.task_type,
            selected_steps=flow.state.selected_steps,
        )
        cli_scope = scope_view(flow.state.context)
        cli_usage = history_cmd._state_usage_payload(tmp_path, flow)

        assert cli_plan_mode == daemon_plan_mode
        assert cli_scope == daemon_scope
        assert cli_usage["summary"] == daemon_usage
        assert cli_usage["calls"] == [
            call for call in cli_usage["calls"] if call["call_id"] == "c1"
        ]

    def test_legacy_engine_projection_consistent_without_usage(self, tmp_path):
        engine = self._write_engine(
            tmp_path,
            plan_context={},
            scope_state=None,
        )
        engine["state"]["selected_steps"] = [
            "analyze",
            "plan",
            "confirm",
            "implement",
            "test",
            "self_check",
        ]
        # A legacy flow with no plan-mode fields and no records: both surfaces
        # infer the same planned path, and the old synthesized all-zero
        # five-field tally surfaces as legacy_ambiguous (unknown/partial) on
        # BOTH surfaces — never silently omitted.
        engine["state"].pop("session_usage_records")
        engine["state"]["steps"]["01_implement_x"]["outputs"] = {"files_changed": []}
        daemon_plan_mode, daemon_scope, daemon_usage = (
            DaemonAggregator._projection_fields(tmp_path, engine)
        )
        assert daemon_plan_mode["legacy_strategy"] == "planned"
        assert daemon_plan_mode["inferred"] is True
        assert daemon_scope is None
        assert daemon_usage is not None
        assert daemon_usage["completeness"] == "partial"

        from tianluo.commands import history_cmd
        from tianluo.engine.persistence import PersistenceManager
        from tianluo.strategy_view import plan_mode_view, scope_view

        (tmp_path / "tianluo" / "state" / "engine.json").write_text(
            json.dumps(engine), encoding="utf-8"
        )
        flow = PersistenceManager(tmp_path).load_flow()
        assert flow is not None
        cli_plan_mode = plan_mode_view(
            flow.state.context,
            task_type=flow.task_type,
            selected_steps=flow.state.selected_steps,
        )
        assert cli_plan_mode == daemon_plan_mode
        assert scope_view(flow.state.context) is None
        cli_usage = history_cmd._state_usage_payload(tmp_path, flow)
        assert cli_usage["completeness"] == daemon_usage["completeness"] == "partial"

    def test_modern_flow_before_first_call_reports_no_usage_on_both_surfaces(
        self, tmp_path,
    ):
        """A modern flow with an empty ledger has made zero LLM calls.

        Neither surface may adapt the all-zero legacy tally into a
        legacy_ambiguous "unknown usage" call — that would claim one unknown
        call for a flow that never issued one.
        """
        engine = self._write_engine(tmp_path, plan_context={}, scope_state=None)
        engine["state"]["session_usage_records"] = []
        engine["state"]["steps"]["01_implement_x"]["outputs"] = {"files_changed": []}
        (tmp_path / "tianluo" / "state" / "engine.json").write_text(
            json.dumps(engine), encoding="utf-8"
        )

        _plan_mode, _scope, daemon_usage = DaemonAggregator._projection_fields(
            tmp_path, engine
        )
        assert daemon_usage is None

        from tianluo.commands import history_cmd
        from tianluo.engine.persistence import PersistenceManager

        flow = PersistenceManager(tmp_path).load_flow()
        assert flow is not None
        cli_usage = history_cmd._state_usage_payload(tmp_path, flow)
        assert cli_usage["summary"] is None
        assert cli_usage["calls"] == []
        assert cli_usage["completeness"] == "none"

    def test_pre_ledger_tally_survives_a_modern_re_save(self, tmp_path):
        """Re-saving a pre-ledger flow must not erase its usage.

        The modern serializer always writes ``session_usage_records`` — for a
        record-less legacy flow that is an EMPTY list beside a still non-zero
        five-field tally. If the surfaces keyed the legacy adaptation on the
        key's absence alone, the first state transition / end-session / archive
        of a resumed pre-12.x flow would make its accumulated usage vanish from
        the daemon snapshot, the WebUI and ``luo history show``.
        """
        from tianluo.commands import history_cmd
        from tianluo.engine.persistence import PersistenceManager

        engine = self._write_engine(tmp_path, plan_context={}, scope_state=None)
        engine["state"].pop("session_usage_records")
        engine["state"]["session_token_usage"] = {
            "input_tokens": 5000,
            "output_tokens": 400,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 200,
            "total_cost_usd": 1.23,
        }
        engine["state"]["steps"]["01_implement_x"]["outputs"] = {"files_changed": []}
        state_file = tmp_path / "tianluo" / "state" / "engine.json"
        state_file.write_text(json.dumps(engine), encoding="utf-8")

        _s, _c, before_daemon = DaemonAggregator._projection_fields(tmp_path, engine)
        flow = PersistenceManager(tmp_path).load_flow()
        assert flow is not None
        assert flow.state.legacy_usage_ledger is True
        before_cli = history_cmd._state_usage_payload(tmp_path, flow)
        assert before_daemon is not None
        assert before_daemon["totals"]["logical_input_tokens"] == 5300
        assert before_daemon["actual_cost_usd"] == pytest.approx(1.23)

        # The modern serializer round trip: an empty ledger list now sits
        # beside the untouched legacy tally.
        engine["state"] = flow.state.to_dict()
        assert engine["state"]["session_usage_records"] == []
        assert engine["state"]["session_token_usage"]["input_tokens"] == 5000
        state_file.write_text(json.dumps(engine), encoding="utf-8")

        _s2, _c2, after_daemon = DaemonAggregator._projection_fields(tmp_path, engine)
        reloaded = PersistenceManager(tmp_path).load_flow()
        assert reloaded is not None
        assert reloaded.state.legacy_usage_ledger is True
        after_cli = history_cmd._state_usage_payload(tmp_path, reloaded)

        assert after_daemon == before_daemon
        assert after_cli == before_cli
        assert after_cli["summary"]["totals"]["logical_input_tokens"] == 5300
        assert after_cli["summary"]["actual_cost_usd"] == pytest.approx(1.23)
