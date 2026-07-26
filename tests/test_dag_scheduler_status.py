"""Tests for the DAGScheduler per-group status lifecycle hook.

Covers the optional ``on_group_status`` callback added to surface live
per-group progress (queued / running / completed / failed / skipped) while
groups run in isolated worktrees. The callback is injected by implement.py;
here it is exercised with an in-memory collector and a faulty callback.
"""

from __future__ import annotations

import threading

from tianluo.engine.dag_scheduler import (
    GROUP_STATUS_COMPLETED,
    GROUP_STATUS_FAILED,
    GROUP_STATUS_QUEUED,
    GROUP_STATUS_RUNNING,
    GROUP_STATUS_SKIPPED,
    DAGScheduler,
    GroupResult,
)


def _completed(group_id: str) -> GroupResult:
    return GroupResult(group_id=group_id, status="completed")


class _Collector:
    """Thread-safe recorder of (group_id, status) emissions in order."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[tuple[str, str]] = []

    def __call__(self, group_id: str, status: str) -> None:
        with self._lock:
            self.events.append((group_id, status))

    def statuses_for(self, group_id: str) -> list[str]:
        with self._lock:
            return [s for g, s in self.events if g == group_id]


class TestStatusEmission:
    def test_success_chain_queued_running_completed(self):
        """A successful group goes through queued → running → completed."""
        groups = [
            {"group_id": "A", "depends_on": []},
            {"group_id": "B", "depends_on": ["A"]},
        ]
        collector = _Collector()

        def execute(group, deps_results, relay_context=None):
            return _completed(group["group_id"])

        DAGScheduler(groups, on_group_status=collector).run(execute)

        for gid in ("A", "B"):
            assert collector.statuses_for(gid) == [
                GROUP_STATUS_QUEUED,
                GROUP_STATUS_RUNNING,
                GROUP_STATUS_COMPLETED,
            ]

    def test_terminal_status_emitted_once_per_group(self):
        """No group emits the same terminal status more than once."""
        groups = [
            {"group_id": "A", "depends_on": []},
            {"group_id": "B", "depends_on": ["A"]},
            {"group_id": "C", "depends_on": ["A"]},
        ]
        collector = _Collector()

        DAGScheduler(groups, on_group_status=collector).run(
            lambda g, d, rc=None: _completed(g["group_id"])
        )

        for gid in ("A", "B", "C"):
            statuses = collector.statuses_for(gid)
            # Each lifecycle status appears exactly once.
            assert statuses.count(GROUP_STATUS_QUEUED) == 1
            assert statuses.count(GROUP_STATUS_RUNNING) == 1
            assert statuses.count(GROUP_STATUS_COMPLETED) == 1

    def test_failure_emits_failed_and_downstream_skipped(self):
        """Upstream failure → failed; transitive downstream → skipped."""
        groups = [
            {"group_id": "A", "depends_on": []},
            {"group_id": "B", "depends_on": ["A"]},
            {"group_id": "C", "depends_on": ["B"]},
        ]
        collector = _Collector()

        def execute(group, deps_results, relay_context=None):
            gid = group["group_id"]
            if gid == "A":
                raise RuntimeError("boom")
            return _completed(gid)

        DAGScheduler(groups, on_group_status=collector).run(execute)

        assert GROUP_STATUS_FAILED in collector.statuses_for("A")
        assert GROUP_STATUS_COMPLETED not in collector.statuses_for("A")
        # Downstream groups never ran — they were skipped, not running/completed.
        assert collector.statuses_for("B") == [
            GROUP_STATUS_QUEUED,
            GROUP_STATUS_SKIPPED,
        ]
        assert collector.statuses_for("C") == [
            GROUP_STATUS_QUEUED,
            GROUP_STATUS_SKIPPED,
        ]

    def test_returned_failed_status_emits_failed(self):
        """A GroupResult.failed() (no exception) still emits 'failed'."""
        groups = [
            {"group_id": "A", "depends_on": []},
            {"group_id": "B", "depends_on": ["A"]},
        ]
        collector = _Collector()

        def execute(group, deps_results, relay_context=None):
            gid = group["group_id"]
            if gid == "A":
                return GroupResult.failed(gid, "internal error")
            return _completed(gid)

        DAGScheduler(groups, on_group_status=collector).run(execute)

        assert GROUP_STATUS_FAILED in collector.statuses_for("A")
        assert collector.statuses_for("B") == [
            GROUP_STATUS_QUEUED,
            GROUP_STATUS_SKIPPED,
        ]


class TestCallbackRobustness:
    def test_no_callback_is_default(self):
        """Omitting on_group_status keeps the previous behavior intact."""
        groups = [
            {"group_id": "A", "depends_on": []},
            {"group_id": "B", "depends_on": ["A"]},
        ]
        results = DAGScheduler(groups).run(
            lambda g, d, rc=None: _completed(g["group_id"])
        )
        result_map = {r.group_id: r for r in results}
        assert result_map["A"].status == "completed"
        assert result_map["B"].status == "completed"

    def test_callback_exception_does_not_break_run(self):
        """A faulty callback never disrupts scheduling or results."""
        groups = [
            {"group_id": "A", "depends_on": []},
            {"group_id": "B", "depends_on": ["A"]},
        ]

        def boom(group_id, status):
            raise ValueError("callback exploded")

        results = DAGScheduler(groups, on_group_status=boom).run(
            lambda g, d, rc=None: _completed(g["group_id"])
        )
        result_map = {r.group_id: r for r in results}
        assert result_map["A"].status == "completed"
        assert result_map["B"].status == "completed"
