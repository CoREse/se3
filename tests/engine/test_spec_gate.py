"""Tests for mechanism A: the SPEC_GATE step handler.

The SPEC_GATE step sits between ``update_spec`` and ``version_analyze`` in the
feature / discovery sequences. It performs two phases:

1. A programmatic artifact check (``validate_spec_structure`` + requirement
   non-decrease for edited specs) — an invalid artifact routes back to
   ``update_spec``.
2. A full re-test through the shared ``run_and_classify_tests`` core — an
   introduced failure routes to ``implement``.

No spec change → no-op COMPLETED.

The artifact-check branches drive the real handler against on-disk spec files
(so ``validate_spec_structure`` / ``parse_spec`` run for real); the re-test
branches patch ``run_and_classify_tests`` so no subprocess is launched.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from se3.engine.models import FlowInstance, Step, StepStatus, StepType
from se3.engine.steps.spec_gate import (
    build_spec_requirement_baseline,
    spec_gate_handler,
)
from se3.engine.steps.test import TestVerdict as _TestVerdict


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

VALID_SPEC = """<!-- spec-format: v1 -->
# my-feature Specification

## Purpose

Defines the my-feature behaviour for the project.

## Requirements

### Requirement: Alpha
- The system SHALL do alpha.

### Requirement: Beta
- The system SHALL do beta.
"""


def _write_spec(project_root, name: str, content: str) -> None:
    spec_dir = project_root / "se3" / "specs" / name
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text(content, encoding="utf-8")


def _read_spec(project_root, name: str) -> str:
    return (project_root / "se3" / "specs" / name / "spec.md").read_text(encoding="utf-8")


def _make_flow(project_root, *, snapshot=None) -> FlowInstance:
    flow = FlowInstance(task_description="spec gate task")
    flow.change_path = project_root / "se3.yaml"
    if snapshot is not None:
        flow.state.context["spec_requirement_baseline"] = snapshot
    return flow


def _make_step(**inputs) -> Step:
    step = Step(step_type=StepType.SPEC_GATE)
    step.inputs = dict(inputs)
    return step


def _green_verdict() -> _TestVerdict:
    return _TestVerdict(
        test_results={"tests_blocking": False, "introduced_failures": []},
        overall_passed=True,
        should_fix=False,
    )


def _red_verdict() -> _TestVerdict:
    return _TestVerdict(
        test_results={
            "tests_blocking": True,
            "introduced_failures": ["tests/test_spec_format.py::test_count"],
        },
        overall_passed=False,
        should_fix=True,
        fix_instructions="Tests are failing. Fix the implementation.",
        fix_context={"reason": "test_failure", "test_failed": True},
    )


# ---------------------------------------------------------------------------
# No-op branch
# ---------------------------------------------------------------------------

class TestNoOp:
    def test_no_spec_change_completes_skipped(self, tmp_path):
        _write_spec(tmp_path, "my-feature", VALID_SPEC)
        snapshot = build_spec_requirement_baseline(tmp_path)
        flow = _make_flow(tmp_path, snapshot=snapshot)
        step = _make_step()

        # No disk change since the snapshot.
        status = spec_gate_handler(step, flow)

        assert status == StepStatus.COMPLETED
        assert step.outputs["gate_passed"] is True
        assert step.outputs["gate_skipped"] is True
        assert step.outputs["fix_needed"] is False
        assert step.outputs["gate_route"] == ""

    def test_missing_snapshot_skips_gate(self, tmp_path):
        _write_spec(tmp_path, "my-feature", VALID_SPEC)
        flow = _make_flow(tmp_path)  # no snapshot in context
        step = _make_step()

        status = spec_gate_handler(step, flow)

        assert status == StepStatus.COMPLETED
        assert step.outputs["gate_passed"] is True
        assert step.outputs["fix_needed"] is False


# ---------------------------------------------------------------------------
# Artifact-invalid branch → route to update_spec
# ---------------------------------------------------------------------------

class TestArtifactInvalid:
    def test_structural_failure_routes_to_update_spec(self, tmp_path):
        _write_spec(tmp_path, "my-feature", VALID_SPEC)
        snapshot = build_spec_requirement_baseline(tmp_path)
        flow = _make_flow(tmp_path, snapshot=snapshot)
        step = _make_step()

        # Corrupt the spec: drop the v1 marker (structural failure) but change
        # content so it is detected as edited.
        _write_spec(
            tmp_path, "my-feature",
            VALID_SPEC.replace("<!-- spec-format: v1 -->\n", ""),
        )

        status = spec_gate_handler(step, flow)

        assert status == StepStatus.REVISION_NEEDED
        assert step.outputs["gate_passed"] is False
        assert step.outputs["gate_route"] == "update_spec"
        assert step.outputs["fix_needed"] is True
        fc = step.outputs["fix_context"]
        assert fc["reason"] == "spec_gate_artifact_invalid"
        assert fc["gate_route"] == "update_spec"
        assert "my-feature" in fc["edited_specs"]
        assert any("v1 marker" in e for e in fc["spec_errors"])

    def test_requirement_deletion_routes_to_update_spec(self, tmp_path):
        _write_spec(tmp_path, "my-feature", VALID_SPEC)
        snapshot = build_spec_requirement_baseline(tmp_path)
        flow = _make_flow(tmp_path, snapshot=snapshot)
        step = _make_step()

        # Remove the Beta requirement — a requirement deletion.
        reduced = VALID_SPEC.split("### Requirement: Beta")[0].rstrip() + "\n"
        _write_spec(tmp_path, "my-feature", reduced)

        status = spec_gate_handler(step, flow)

        assert status == StepStatus.REVISION_NEEDED
        assert step.outputs["gate_route"] == "update_spec"
        errors = step.outputs["fix_context"]["spec_errors"]
        assert any("Beta" in e for e in errors), errors
        assert any("removed requirement" in e or "lost requirements" in e for e in errors)

    def test_invalid_new_spec_routes_to_update_spec(self, tmp_path):
        _write_spec(tmp_path, "my-feature", VALID_SPEC)
        snapshot = build_spec_requirement_baseline(tmp_path)
        flow = _make_flow(tmp_path, snapshot=snapshot)
        step = _make_step()

        # A brand-new spec that is structurally invalid (no v1 marker / title).
        _write_spec(tmp_path, "fresh-spec", "just some prose, not a spec\n")

        status = spec_gate_handler(step, flow)

        assert status == StepStatus.REVISION_NEEDED
        assert step.outputs["gate_route"] == "update_spec"
        fc = step.outputs["fix_context"]
        assert "fresh-spec" in fc["new_specs"]
        assert any("fresh-spec" in e for e in fc["spec_errors"])


# ---------------------------------------------------------------------------
# Artifact-clean → full re-test branch
# ---------------------------------------------------------------------------

class TestReTest:
    def _edited_valid(self, tmp_path):
        """Set up an edited-but-valid spec (a new requirement appended) plus a
        flow whose snapshot predates the edit."""
        _write_spec(tmp_path, "my-feature", VALID_SPEC)
        snapshot = build_spec_requirement_baseline(tmp_path)
        # Append a requirement → content differs (edited) and the requirement
        # set grows (non-decrease satisfied), so the artifact check passes.
        grown = VALID_SPEC + "\n### Requirement: Gamma\n- The system SHALL do gamma.\n"
        _write_spec(tmp_path, "my-feature", grown)
        flow = _make_flow(tmp_path, snapshot=snapshot)
        return flow

    def test_clean_artifact_green_retest_completes(self, tmp_path):
        flow = self._edited_valid(tmp_path)
        step = _make_step(baseline_failures=[])

        with patch("se3.config.TestConfig") as mock_tc, \
             patch("se3.engine.steps.test.run_and_classify_tests",
                   return_value=_green_verdict()) as mock_run:
            mock_tc.load.return_value = MagicMock()
            status = spec_gate_handler(step, flow)

        assert status == StepStatus.COMPLETED
        assert step.outputs["gate_passed"] is True
        assert step.outputs["gate_route"] == ""
        assert step.outputs["fix_needed"] is False
        # Full suite (not a fix-iteration subset) is requested at the gate.
        assert mock_run.call_args.kwargs["is_fix_iteration"] is False

    def test_clean_artifact_red_retest_routes_to_implement(self, tmp_path):
        flow = self._edited_valid(tmp_path)
        step = _make_step(baseline_failures=[])

        with patch("se3.config.TestConfig") as mock_tc, \
             patch("se3.engine.steps.test.run_and_classify_tests",
                   return_value=_red_verdict()):
            mock_tc.load.return_value = MagicMock()
            status = spec_gate_handler(step, flow)

        assert status == StepStatus.REVISION_NEEDED
        assert step.outputs["gate_passed"] is False
        assert step.outputs["gate_route"] == "implement"
        assert step.outputs["fix_needed"] is True
        assert "failing" in step.outputs["fix_instructions"].lower()
        assert step.outputs["fix_context"]["gate_route"] == "implement"

    def test_baseline_failures_forwarded_to_retest(self, tmp_path):
        flow = self._edited_valid(tmp_path)
        baseline = ["tests/test_x.py::test_old"]
        step = _make_step(baseline_failures=baseline)

        with patch("se3.config.TestConfig") as mock_tc, \
             patch("se3.engine.steps.test.run_and_classify_tests",
                   return_value=_green_verdict()) as mock_run:
            mock_tc.load.return_value = MagicMock()
            spec_gate_handler(step, flow)

        assert mock_run.call_args.kwargs["baseline_failures"] == baseline


# ---------------------------------------------------------------------------
# Snapshot builder
# ---------------------------------------------------------------------------

class TestSnapshotBuilder:
    def test_builds_content_and_requirement_names(self, tmp_path):
        _write_spec(tmp_path, "my-feature", VALID_SPEC)
        snap = build_spec_requirement_baseline(tmp_path)

        assert "my-feature" in snap
        assert snap["my-feature"]["content"] == VALID_SPEC
        assert snap["my-feature"]["requirements"] == ["Alpha", "Beta"]

    def test_new_spec_only_structural_no_decrease_check(self, tmp_path):
        """A valid new spec passes even though it has no prior baseline."""
        _write_spec(tmp_path, "my-feature", VALID_SPEC)
        snapshot = build_spec_requirement_baseline(tmp_path)
        flow = _make_flow(tmp_path, snapshot=snapshot)
        step = _make_step(baseline_failures=[])

        # Add a valid new spec (different name → "new", not "edited").
        new_spec = VALID_SPEC.replace("my-feature", "another-feature")
        _write_spec(tmp_path, "another-feature", new_spec)

        with patch("se3.config.TestConfig") as mock_tc, \
             patch("se3.engine.steps.test.run_and_classify_tests",
                   return_value=_green_verdict()):
            mock_tc.load.return_value = MagicMock()
            status = spec_gate_handler(step, flow)

        assert status == StepStatus.COMPLETED
        assert step.outputs["gate_passed"] is True
