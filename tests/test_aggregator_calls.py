"""Tests for ``DaemonAggregator._enumerate_calls`` pending-call filtering.

Regression coverage for the bug where an already-answered confirm call (its
``.json`` request and ``.response`` answer both lingering in ``se3/calls/``)
was perpetually reported to the web UI as "needs response".
"""

from __future__ import annotations

import json
from pathlib import Path

from se3.daemon.aggregator import DaemonAggregator


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _make_calls_dir(tmp_path: Path) -> Path:
    """Create a project root with a populated ``se3/calls/`` directory."""
    calls = tmp_path / "se3" / "calls"
    calls.mkdir(parents=True)

    # 1. An unanswered call — no sibling response file.
    _write(calls / "confirm_pending_001.json", {"step_to_review_id": "s1"})

    # 2. An answered call with a ``.response`` sibling.
    _write(calls / "confirm_answered_002.json", {"step_to_review_id": "s2"})
    _write(
        calls / "confirm_answered_002.response",
        {"approved": True, "feedback": "looks good"},
    )

    # 3. An answered call with a ``.response.json`` sibling.
    _write(calls / "confirm_answered_003.json", {"step_to_review_id": "s3"})
    _write(
        calls / "confirm_answered_003.response.json",
        {"approved": True, "feedback": "ok"},
    )

    return tmp_path


def test_enumerate_calls_skips_answered_and_response_files(tmp_path: Path) -> None:
    root = _make_calls_dir(tmp_path)
    aggregator = DaemonAggregator()

    calls = aggregator._enumerate_calls(root)
    call_ids = {c.call_id for c in calls}

    # Only the genuinely-pending call is returned.
    assert call_ids == {"confirm_pending_001"}

    # The one returned call is reported as a "call" kind.
    assert len(calls) == 1
    assert calls[0].kind == "call"
    assert calls[0].project_root == str(root)


def test_enumerate_calls_does_not_emit_response_files(tmp_path: Path) -> None:
    root = _make_calls_dir(tmp_path)
    aggregator = DaemonAggregator()

    calls = aggregator._enumerate_calls(root)

    # Response files themselves are answers, never pending calls.
    for call in calls:
        assert not call.path.endswith(".response")
        assert not call.path.endswith(".response.json")


def test_enumerate_calls_returns_unanswered_calls(tmp_path: Path) -> None:
    calls_dir = tmp_path / "se3" / "calls"
    calls_dir.mkdir(parents=True)
    _write(calls_dir / "confirm_a.json", {"step_to_review_id": "a"})
    _write(calls_dir / "confirm_b.json", {"step_to_review_id": "b"})

    aggregator = DaemonAggregator()
    calls = aggregator._enumerate_calls(tmp_path)

    assert {c.call_id for c in calls} == {"confirm_a", "confirm_b"}


def test_enumerate_calls_missing_dir(tmp_path: Path) -> None:
    aggregator = DaemonAggregator()
    assert aggregator._enumerate_calls(tmp_path) == []
