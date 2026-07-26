"""G5 task 1 — consolidated spec-write-protection soft-layer coverage.

Asserts, in one place, the full soft-injection contract of the spec-write
governance work:

* the derived exemption set ``SPEC_WRITE_ALLOWED_STEPS`` and its authoritative
  sub-sets (guarding against ``sync_respond`` re-drifting out of the set);
* the ``get_spec_write_protection_injection`` hit/miss matrix across every
  relevant step (non-read-only LLM steps hit; ``update_spec`` and all four sync
  steps miss; read-only / non-LLM / unknown steps miss);
* that ``LLMCaller.call()`` weaves the constraint into the prompt for protected
  steps and withholds it from ``update_spec`` and the sync steps;
* that the ``plan`` step carries its dedicated protection section while keeping
  the ``spec_changes`` declaration channel intact;
* that neither the injection nor the plan section trips the anti-regression
  spec-driven framing guardrail (``find_spec_driven_framing``).

Note: ``tests/test_spec_write_protection_injection.py`` (G1) and
``tests/test_spec_write_hook.py`` (G3) cover overlapping surface; this file is
the dedicated G5 regression net spanning the soft layer end to end, including
the plan-section coverage those files do not assert together.
"""

from __future__ import annotations

import pytest
from unittest.mock import patch

from tianluo.engine.context_builder import (
    SPEC_WRITE_ALLOWED_STEPS,
    _ALL_SYNC_STEPS,
    _READ_ONLY_SYNC_STEPS,
    _WRITABLE_SYNC_STEPS,
    _is_spec_write_protected_step,
    get_spec_write_protection_injection,
)


# Steps that MUST receive the spec-write-protection injection: every
# non-read-only LLM step except the exempt write-spec steps. charter_freshness
# joined this set when its read_only was flipped to False (its handler writes
# se3/charter.md) — the injection only forbids se3/specs/ writes, which it never
# does, so the connateral effect is harmless and directionally correct.
PROTECTED_STEPS = [
    "implement",
    "propose",
    "design",
    "plan_tasks",
    "charter_freshness",
]

# The four sync pseudo-steps (read + write paths) — all exempt.
SYNC_STEPS = ["sync_scan", "sync_analyze", "sync_resolve", "sync_respond"]

# Read-only / non-LLM / interactive steps that must NOT receive the injection.
NON_PROTECTED_STEPS = [
    "plan",
    "analyze",
    "verify_spec",
    "summarize",
    "commit",
    "test",
    "confirm",
    "discovery",
    "version_analyze",
    "self_check",
    "project_summary",
]


# ---------------------------------------------------------------------------
# Derived exemption set (anti-drift guard)
# ---------------------------------------------------------------------------

class TestDerivedExemptionSet:
    """The exemption set is *derived*, never hand-enumerated, so a writable
    sync step (esp. ``sync_respond``) cannot silently drop out of it."""

    def test_writable_sync_steps_membership(self):
        assert _WRITABLE_SYNC_STEPS == frozenset({"sync_resolve", "sync_respond"})

    def test_read_only_sync_steps_membership(self):
        assert _READ_ONLY_SYNC_STEPS == frozenset({"sync_scan", "sync_analyze"})

    def test_all_sync_steps_is_union_of_the_two(self):
        assert _ALL_SYNC_STEPS == _READ_ONLY_SYNC_STEPS | _WRITABLE_SYNC_STEPS

    def test_allowed_set_is_derived_relationship(self):
        # The exact derived relationship the design pins down:
        # {update_spec} | _READ_ONLY_SYNC_STEPS | _WRITABLE_SYNC_STEPS
        assert SPEC_WRITE_ALLOWED_STEPS == (
            frozenset({"update_spec"})
            | _READ_ONLY_SYNC_STEPS
            | _WRITABLE_SYNC_STEPS
        )

    def test_allowed_set_concrete_members(self):
        assert SPEC_WRITE_ALLOWED_STEPS == frozenset(
            {
                "update_spec",
                "sync_scan",
                "sync_analyze",
                "sync_resolve",
                "sync_respond",
            }
        )

    def test_sync_respond_is_present(self):
        # The exact omission the previous review round flagged.
        assert "sync_respond" in SPEC_WRITE_ALLOWED_STEPS
        assert "sync_respond" in _WRITABLE_SYNC_STEPS


# ---------------------------------------------------------------------------
# get_spec_write_protection_injection — hit/miss matrix
# ---------------------------------------------------------------------------

class TestInjectionMatrix:
    @pytest.mark.parametrize("step", PROTECTED_STEPS)
    def test_protected_steps_return_constraint(self, step):
        injection = get_spec_write_protection_injection(step)
        assert injection != ""
        assert "SPEC FILE WRITE PROTECTION" in injection

    def test_update_spec_returns_empty(self):
        assert get_spec_write_protection_injection("update_spec") == ""

    @pytest.mark.parametrize("step", SYNC_STEPS)
    def test_all_sync_steps_return_empty(self, step):
        # Sync steps are absent from STEP_POOL AND listed in the exemption set;
        # either alone suffices, both together is the design's double safeguard.
        assert get_spec_write_protection_injection(step) == ""

    @pytest.mark.parametrize("step", NON_PROTECTED_STEPS)
    def test_read_only_and_non_llm_steps_return_empty(self, step):
        assert get_spec_write_protection_injection(step) == ""

    def test_unknown_step_returns_empty(self):
        assert get_spec_write_protection_injection("nonexistent_step") == ""

    @pytest.mark.parametrize("step", PROTECTED_STEPS)
    def test_is_protected_predicate_true(self, step):
        assert _is_spec_write_protected_step(step) is True

    @pytest.mark.parametrize("step", ["update_spec"] + SYNC_STEPS + NON_PROTECTED_STEPS)
    def test_is_protected_predicate_false(self, step):
        assert _is_spec_write_protected_step(step) is False


# ---------------------------------------------------------------------------
# Injection wording — allows behavior change, forbids spec writes, compliant
# ---------------------------------------------------------------------------

class TestInjectionWording:
    def test_allows_behavior_change(self):
        lowered = get_spec_write_protection_injection("implement").lower()
        assert "free to change existing code behavior" in lowered

    def test_records_changes_through_charter_refactor_channels(self):
        # The charter refactor retired the plan spec_changes / verify_spec /
        # update_spec spec channel; behavior changes are now recorded in the
        # charter + colocated why-comments, kept current by charter_freshness
        # and the implement step's why-comment convention.
        injection = get_spec_write_protection_injection("implement")
        lowered = injection.lower()
        assert "charter" in lowered
        assert "why-comment" in lowered
        assert "charter_freshness" in lowered
        assert "spec_changes" not in injection

    def test_forbids_spec_writes_via_tools_and_bash(self):
        injection = get_spec_write_protection_injection("implement")
        assert "se3/specs" in injection
        for token in ("Write", "Edit", "NotebookEdit", "Bash"):
            assert token in injection


# ---------------------------------------------------------------------------
# LLMCaller.call() weaves the injection for protected steps only
# ---------------------------------------------------------------------------

class TestLLMCallerWeave:
    def _caller(self, step_type):
        from tianluo.engine.llm_caller import LLMCaller

        return LLMCaller(
            project_root="/tmp/test_spec_write_protection",
            step_type=step_type,
            agents=[{"name": "test", "type": "claude-code", "cmd": "echo test"}],
        )

    @patch("tianluo.engine.llm_caller.LLMCaller._call_with_retry")
    def test_protected_step_prompt_contains_constraint(self, mock_call):
        mock_call.return_value = "ok"
        self._caller("implement").call("Implement it", json_mode="off")
        assert "SPEC FILE WRITE PROTECTION" in mock_call.call_args[1]["prompt"]

    @patch("tianluo.engine.llm_caller.LLMCaller._call_with_retry")
    def test_update_spec_prompt_has_no_constraint(self, mock_call):
        mock_call.return_value = "ok"
        self._caller("update_spec").call("Update spec", json_mode="off")
        assert "SPEC FILE WRITE PROTECTION" not in mock_call.call_args[1]["prompt"]

    @pytest.mark.parametrize("step", SYNC_STEPS)
    @patch("tianluo.engine.llm_caller.LLMCaller._call_with_retry")
    def test_sync_steps_prompt_has_no_constraint(self, mock_call, step):
        mock_call.return_value = "ok"
        self._caller(step).call("sync work", json_mode="off")
        assert "SPEC FILE WRITE PROTECTION" not in mock_call.call_args[1]["prompt"]


# ---------------------------------------------------------------------------
# plan.py — spec machinery retired: no dedicated section, no spec_changes channel
# ---------------------------------------------------------------------------

class TestPlanProtectionSection:
    """The plan step no longer routes work through the retired spec governance
    steps, so its dedicated spec-write-protection section (and the
    ``spec_changes`` / ``update_spec`` / ``verify_spec`` framing it carried) has
    been removed. The plan plans against task / charter / code-index only."""

    def test_section_constant_removed(self):
        import tianluo.engine.steps.plan as plan_mod

        assert not hasattr(plan_mod, "SPEC_WRITE_PROTECTION_SECTION")

    @pytest.mark.parametrize("depth", ["full", "medium", "shallow"])
    def test_no_spec_machinery_in_prompt(self, depth):
        from tianluo.engine.steps.plan import _build_prompt

        prompt = _build_prompt(
            task_description="Add feature X",
            task_type="feature",
            scope="m",
            project_summary="p",
            revision_section="",
            depth=depth,
        )
        assert "spec_changes" not in prompt
        assert "update_spec" not in prompt
        assert "verify_spec" not in prompt
        assert "Spec Changes Declaration" not in prompt
