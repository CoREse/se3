"""Engine-side tests for the ``merging`` sub-state (design G1).

Mirrors the ``waiting_for_lock`` coverage: ``merging`` is a top-level
emit-when-True boolean on :class:`FlowInstance` that layers on a COMPLETED
worktree flow while its branch is being merged back, plus a ``record_merging``
chat-history lifecycle anchor. These tests pin the emit-when-True /
backward-compatible round-trip and the jsonl shape so the propagation and
presentation layers downstream can rely on the data base being present.
"""

from __future__ import annotations

import json

from se3.engine.chat_history import record_merging
from se3.engine.models import FlowInstance, FlowStatus


class TestFlowInstanceMerging:
    def test_default_is_false(self):
        flow = FlowInstance(task_description="t", status=FlowStatus.COMPLETED)
        assert flow.merging is False

    def test_to_dict_omits_key_when_false(self):
        # emit-when-True: an ordinary (non-merging) engine.json must stay free
        # of the key so old readers ignore the absent key.
        flow = FlowInstance(task_description="t", status=FlowStatus.COMPLETED)
        assert "merging" not in flow.to_dict()

    def test_to_dict_emits_true_when_merging(self):
        flow = FlowInstance(task_description="t", status=FlowStatus.COMPLETED)
        flow.merging = True
        assert flow.to_dict()["merging"] is True

    def test_round_trips_true_through_from_dict(self):
        flow = FlowInstance(task_description="t", status=FlowStatus.COMPLETED)
        flow.merging = True
        restored = FlowInstance.from_dict(flow.to_dict())
        assert restored.merging is True

    def test_round_trips_false_through_from_dict(self):
        flow = FlowInstance(task_description="t", status=FlowStatus.COMPLETED)
        restored = FlowInstance.from_dict(flow.to_dict())
        assert restored.merging is False

    def test_legacy_dict_without_key_reads_as_false(self):
        # Old engine.json files predate the field; a missing key must read as
        # False rather than raising.
        flow = FlowInstance(task_description="t", status=FlowStatus.COMPLETED)
        data = flow.to_dict()
        data.pop("merging", None)
        assert FlowInstance.from_dict(data).merging is False

    def test_merging_and_waiting_for_lock_are_orthogonal(self):
        # merging && waiting_for_lock: the merge itself is blocked queueing for
        # the main-worktree lock — both must independently survive a round-trip.
        flow = FlowInstance(task_description="t", status=FlowStatus.COMPLETED)
        flow.merging = True
        flow.waiting_for_lock = True
        restored = FlowInstance.from_dict(flow.to_dict())
        assert restored.merging is True
        assert restored.waiting_for_lock is True


class TestRecordMerging:
    def test_writes_one_jsonl_line_with_expected_shape(self, tmp_path):
        record_merging(tmp_path, "flow-1", "05_merge_abc", "merge")
        path = tmp_path / "se3" / "history" / "flow-1" / "05_merge_abc.jsonl"
        assert path.exists()
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["type"] == "merging"
        assert record["status"] == "merging"
        assert record["step_id"] == "05_merge_abc"
        assert record["step_type"] == "merge"
        assert "role" not in record  # not a ChatMessage — history readers skip it
        assert record["message"]  # non-empty default message
        assert "timestamp" in record and isinstance(record["timestamp"], str)

    def test_custom_message_is_used(self, tmp_path):
        record_merging(tmp_path, "flow-1", "05_merge_abc", "merge", message="合并中…")
        path = tmp_path / "se3" / "history" / "flow-1" / "05_merge_abc.jsonl"
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert record["message"] == "合并中…"

    def test_write_failure_is_swallowed(self, tmp_path, monkeypatch):
        # A best-effort anchor: an I/O error must never break the running flow.
        import se3.engine.chat_history as ch

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(ch.Path, "mkdir", boom)
        # Must not raise.
        record_merging(tmp_path, "flow-1", "05_merge_abc", "merge")
