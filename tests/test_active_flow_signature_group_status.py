"""End-to-end signature verification for live DAG per-group status pushes.

This module proves the G5 claim of the per-group-status feature: appending a
``group_status`` NDJSON line (written by
:func:`se3.engine.chat_history.record_group_status`) to the main repo's
``se3/history/<flow_id>/<step_id>.jsonl`` shifts the change-detection token
returned by :meth:`se3.daemon.history.DaemonHistoryReader.active_flow_signature`.

That shift is exactly what drives the daemon client's incremental
``_push_history`` → ``MSG_HISTORY_DATA`` chain, so the web console receives each
``group_status`` record *before* the implement step ends instead of staying
blank for the whole parallel phase.

Conclusion (no code change needed): ``active_flow_signature`` already
fingerprints every history ``*.jsonl`` by ``(name, mtime, size)`` (see
``_safe_stat`` in :mod:`se3.daemon.history`). Because each appended line grows
the file's byte size, the token changes on every append even when two writes
land inside the filesystem's mtime resolution. The existing fingerprint covers
this scenario, so :mod:`se3.daemon.history` needs no reinforcement; this test
locks the behavior in. The verification object is deliberately
``src/se3/daemon/history.py`` — the daemon-side reader the client polls — not
``aggregator.py``.
"""

from __future__ import annotations

import json

from se3.daemon.history import DaemonHistoryReader
from se3.engine.chat_history import record_group_status


# Verification target lives in src/se3/daemon/history.py (the daemon-side
# reader polled by the client), not aggregator.py.
assert DaemonHistoryReader.__module__ == "se3.daemon.history"


FLOW_ID = "flow-grp-sig"
STEP_ID = "07_implement_abc123"


def _make_reader(root):
    """Build a reader whose project-roots provider yields *root*."""
    return DaemonHistoryReader(project_roots_provider=lambda: [root])


def _write_active_engine(root, flow_id=FLOW_ID, status="RUNNING"):
    """Write a minimal active ``engine.json`` so the flow is seen as active."""
    state_dir = root / "se3" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "engine.json").write_text(
        json.dumps({"flow_id": flow_id, "status": status}), encoding="utf-8"
    )


def _seed_step_jsonl(root, flow_id=FLOW_ID, step_id=STEP_ID):
    """Seed an existing per-step history jsonl with one chat line."""
    hist_dir = root / "se3" / "history" / flow_id
    hist_dir.mkdir(parents=True, exist_ok=True)
    jsonl = hist_dir / f"{step_id}.jsonl"
    jsonl.write_text(
        json.dumps({"role": "assistant", "content": "implementing..."}) + "\n",
        encoding="utf-8",
    )
    return jsonl


def test_group_status_append_shifts_active_flow_signature(tmp_path):
    """Appending a group_status line changes the flow's signature token."""
    _write_active_engine(tmp_path)
    _seed_step_jsonl(tmp_path)
    reader = _make_reader(tmp_path)

    before = reader.active_flow_signature()
    assert FLOW_ID in before

    record_group_status(
        tmp_path, FLOW_ID, STEP_ID, "implement", "G3", "running"
    )

    after = reader.active_flow_signature()
    assert FLOW_ID in after

    # The (name, mtime, size) fingerprint must move — confirming the append
    # will trigger an incremental history push.
    assert before[FLOW_ID] != after[FLOW_ID]


def test_group_status_shifts_signature_via_size_even_without_mtime_change(
    tmp_path,
):
    """The byte-size component shifts the token even if mtime is unchanged.

    Two writes can land inside the filesystem's mtime resolution; the size
    component of ``_safe_stat`` guarantees the token still moves. We simulate
    that worst case by force-restoring the seeded file's mtime after the
    append, leaving only the size difference.
    """
    _write_active_engine(tmp_path)
    jsonl = _seed_step_jsonl(tmp_path)
    reader = _make_reader(tmp_path)

    before = reader.active_flow_signature()
    seeded_stat = jsonl.stat()

    record_group_status(
        tmp_path, FLOW_ID, STEP_ID, "implement", "G1", "completed"
    )
    # Pin mtime back to the pre-append value so only size differs.
    import os

    os.utime(jsonl, (seeded_stat.st_atime, seeded_stat.st_mtime))

    after = reader.active_flow_signature()
    assert before[FLOW_ID] != after[FLOW_ID]


def test_first_group_status_creates_jsonl_and_shifts_signature(tmp_path):
    """A brand-new step jsonl (first group_status) also shifts the signature.

    When the implement step's jsonl does not yet exist in the main repo, the
    first ``group_status`` line creates it; the new ``(name, mtime, size)``
    entry appears in the signature, shifting the token from its file-absent
    state.
    """
    _write_active_engine(tmp_path)
    # No history jsonl yet — only the engine.json exists.
    (tmp_path / "se3" / "history" / FLOW_ID).mkdir(parents=True, exist_ok=True)
    reader = _make_reader(tmp_path)

    before = reader.active_flow_signature()
    assert FLOW_ID in before

    record_group_status(
        tmp_path, FLOW_ID, STEP_ID, "implement", "G2", "queued"
    )

    after = reader.active_flow_signature()
    assert before[FLOW_ID] != after[FLOW_ID]


def test_terminal_flow_excluded_from_signature(tmp_path):
    """A completed flow is excluded, so its group_status appends do not push.

    This documents the boundary: the live-status channel only matters for an
    active (non-terminal) flow; a terminal flow has nothing left to stream.
    """
    _write_active_engine(tmp_path, status="COMPLETED")
    _seed_step_jsonl(tmp_path)
    reader = _make_reader(tmp_path)

    assert FLOW_ID not in reader.active_flow_signature()
