"""Tests for free-text CONFIRM answer interpretation (incl. Chinese vocab).

The web console offers a single free-text reply box for CONFIRM gates. An
operator approving/rejecting a gate types a word like "approve" / "同意"
rather than a structured payload, and ``_interpret_confirm_answer`` maps that
free text onto ``(approved, feedback)``. These tests lock in:

- English and Chinese approval/rejection words are recognized.
- "1" and other unrecognized text still fall through to a revision request
  carrying the original text as feedback (the web frontend intercepts these
  with an explicit "this will be treated as a revision request" confirmation
  — see G3 — so the backend deliberately keeps the conservative default).
- The structured inner-dict payload ``{approved, feedback}`` is correctly
  unwrapped and drives COMPLETED / REVISION_NEEDED end-to-end via
  ``_check_confirm_response``.
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tianluo.commands.run import _check_confirm_response, _interpret_confirm_answer
from tianluo.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)


# --- Free-text interpretation --------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        # English
        "approve", "approved", "yes", "y", "ok", "okay", "lgtm",
        "accept", "accepted", "continue", "proceed", "pass", "skip",
        # Case / first-word semantics
        "Approve", "APPROVE", "approve looks good",
        # Chinese
        "同意", "通过", "批准", "确认", "允许", "接受",
    ],
)
def test_approval_words(text):
    approved, feedback = _interpret_confirm_answer(text)
    assert approved is True
    assert feedback is None


@pytest.mark.parametrize(
    "text",
    [
        # English
        "no", "n", "reject", "rejected", "deny", "denied",
        "revise", "revision", "changes",
        # Chinese
        "驳回", "拒绝", "打回", "否决", "不通过", "重做", "重拟",
    ],
)
def test_rejection_words(text):
    approved, feedback = _interpret_confirm_answer(text)
    assert approved is False
    # Rejection preserves the operator's original text as feedback.
    assert feedback == text.strip()


@pytest.mark.parametrize(
    "text",
    [
        "1",
        "42",
        "看起来不错",  # plausible-approval-in-spirit but NOT in the whitelist
        "please double check the edge cases",
        "maybe",
    ],
)
def test_unrecognized_text_is_conservative_revision(text):
    """Unknown text must NOT be silently approved; it becomes a revision
    request carrying the original text (the frontend does the second-guess)."""
    approved, feedback = _interpret_confirm_answer(text)
    assert approved is False
    assert feedback == text.strip()


def test_empty_answer_defaults_to_not_approved():
    approved, feedback = _interpret_confirm_answer("")
    assert approved is False
    assert feedback is None


# --- End-to-end: structured inner-dict payload ---------------------------

class TestStructuredPayloadEndToEnd:
    """Assert the ``{"response": {"approved", "feedback"}}`` daemon envelope is
    unwrapped by ``_check_confirm_response`` and drives the state machine."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)
        (self.project_root / "tianluo" / "calls").mkdir(parents=True, exist_ok=True)

        self.flow = FlowInstance(
            task_description="Test task",
            task_type="feature",
            change_name="test-change",
            change_path=self.project_root,
        )
        self.flow.state.selected_steps = [
            StepType.PLAN,
            StepType.CONFIRM,
            StepType.IMPLEMENT,
        ]

        plan_step = Step(
            step_type=StepType.PLAN,
            status=StepStatus.COMPLETED,
            step_id="plan-001",
        )
        self.flow.state.add_step(plan_step)

        self.confirm_step = Step(
            step_type=StepType.CONFIRM,
            status=StepStatus.PAUSED,
            step_id="confirm-001",
            inputs={
                "step_to_review_id": "plan-001",
                "step_to_review_type": "plan",
            },
        )
        self.flow.state.add_step(self.confirm_step)
        self.flow.state.current_step_id = "confirm-001"
        self.flow.status = FlowStatus.PAUSED

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_call_and_response(self, response_data):
        calls_dir = self.project_root / "tianluo" / "calls"
        call_file = calls_dir / "confirm_test.json"
        call_file.write_text(json.dumps({
            "step": self.confirm_step.step_id,
            "change_id": self.flow.change_name,
        }))
        # The daemon writes ``<stem>.response.json`` with the answer nested
        # under a ``response`` key.
        (calls_dir / "confirm_test.response.json").write_text(
            json.dumps(response_data)
        )

    def test_inner_dict_approved_drives_completed(self):
        self._write_call_and_response(
            {"call_id": "c1", "response": {"approved": True, "feedback": None}}
        )
        result = _check_confirm_response(
            self.flow, self.confirm_step, self.project_root
        )
        assert result == StepStatus.COMPLETED
        review = self.confirm_step.outputs["review_result"]
        assert review["approved"] is True
        assert self.confirm_step.outputs["revision_feedback"] is None

    def test_inner_dict_rejected_drives_revision(self):
        self._write_call_and_response(
            {"call_id": "c1", "response": {"approved": False, "feedback": "fix X"}}
        )
        result = _check_confirm_response(
            self.flow, self.confirm_step, self.project_root
        )
        assert result == StepStatus.REVISION_NEEDED
        review = self.confirm_step.outputs["review_result"]
        assert review["approved"] is False
        assert review["feedback"] == "fix X"
        assert self.confirm_step.outputs["revision_feedback"] == "fix X"

    def test_inner_free_text_chinese_approval_drives_completed(self):
        # Web console free-text path: daemon nests a plain string under
        # ``response``; the Chinese approval word must be recognized.
        self._write_call_and_response({"call_id": "c1", "response": "同意"})
        result = _check_confirm_response(
            self.flow, self.confirm_step, self.project_root
        )
        assert result == StepStatus.COMPLETED
        assert self.confirm_step.outputs["review_result"]["approved"] is True

    def test_inner_free_text_one_drives_revision_with_original_text(self):
        # "1" must NOT be silently approved; it lands as a revision request
        # carrying the original text as feedback.
        self._write_call_and_response({"call_id": "c1", "response": "1"})
        result = _check_confirm_response(
            self.flow, self.confirm_step, self.project_root
        )
        assert result == StepStatus.REVISION_NEEDED
        assert self.confirm_step.outputs["review_result"]["approved"] is False
        assert self.confirm_step.outputs["revision_feedback"] == "1"
