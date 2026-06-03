"""Tests for the cross-flow baseline-fix memory (engine/baseline_fix_memory.py).

Covers:
- load_given_up on a missing / corrupt / schema-mismatched store reads empty
- record_given_up atomic write, accumulation of attempts, reason metadata
- LRU bound at MAX_ENTRIES retains the most-recently-touched ids
"""

from __future__ import annotations

import json
from pathlib import Path

from se3.engine import baseline_fix_memory as bfm


# ---------------------------------------------------------------------------
# load_given_up — empty / corruption tolerance
# ---------------------------------------------------------------------------

class TestLoadGivenUp:
    def test_missing_file_reads_empty(self, tmp_path):
        assert bfm.load_given_up(tmp_path) == set()

    def test_roundtrip(self, tmp_path):
        bfm.record_given_up(tmp_path, ["t::a", "t::b"], attempts=2, reason="flaky")
        assert bfm.load_given_up(tmp_path) == {"t::a", "t::b"}

    def test_corrupt_json_reads_empty(self, tmp_path):
        path = bfm.memory_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not valid json", encoding="utf-8")
        assert bfm.load_given_up(tmp_path) == set()

    def test_schema_mismatch_reads_empty(self, tmp_path):
        path = bfm.memory_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"schema_version": 999, "entries": {"t::a": {"attempts": 1}}}),
            encoding="utf-8",
        )
        assert bfm.load_given_up(tmp_path) == set()

    def test_non_mapping_reads_empty(self, tmp_path):
        path = bfm.memory_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        assert bfm.load_given_up(tmp_path) == set()


# ---------------------------------------------------------------------------
# record_given_up — write, accumulation, metadata
# ---------------------------------------------------------------------------

class TestRecordGivenUp:
    def test_atomic_write_leaves_no_tempfile(self, tmp_path):
        bfm.record_given_up(tmp_path, ["t::a"], attempts=1)
        state_dir = bfm.memory_path(tmp_path).parent
        leftovers = [p for p in state_dir.iterdir() if p.name.startswith(".baseline_fix_attempts.")]
        assert leftovers == []

    def test_empty_ids_is_noop(self, tmp_path):
        path = bfm.record_given_up(tmp_path, [], attempts=3, reason="x")
        # No file written for an empty id list.
        assert not path.exists()
        assert bfm.load_given_up(tmp_path) == set()

    def test_attempts_accumulate(self, tmp_path):
        bfm.record_given_up(tmp_path, ["t::a"], attempts=2, reason="first")
        bfm.record_given_up(tmp_path, ["t::a"], attempts=3, reason="second")
        details = bfm.load_given_up_details(tmp_path)
        assert details["t::a"]["attempts"] == 5
        assert details["t::a"]["reason"] == "second"

    def test_reason_preserved_when_none_on_rerecord(self, tmp_path):
        bfm.record_given_up(tmp_path, ["t::a"], attempts=1, reason="env-missing-lib")
        bfm.record_given_up(tmp_path, ["t::a"], attempts=1, reason=None)
        details = bfm.load_given_up_details(tmp_path)
        assert details["t::a"]["attempts"] == 2
        assert details["t::a"]["reason"] == "env-missing-lib"

    def test_other_entries_preserved(self, tmp_path):
        bfm.record_given_up(tmp_path, ["t::a"], attempts=1)
        bfm.record_given_up(tmp_path, ["t::b"], attempts=1)
        assert bfm.load_given_up(tmp_path) == {"t::a", "t::b"}

    def test_negative_attempts_clamped_to_zero(self, tmp_path):
        bfm.record_given_up(tmp_path, ["t::a"], attempts=-5)
        details = bfm.load_given_up_details(tmp_path)
        assert details["t::a"]["attempts"] == 0


# ---------------------------------------------------------------------------
# LRU bound
# ---------------------------------------------------------------------------

class TestLruBound:
    def test_trims_oldest_beyond_cap(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bfm, "MAX_ENTRIES", 3)
        for i in range(5):
            bfm.record_given_up(tmp_path, [f"t::{i}"], attempts=1)
        remaining = bfm.load_given_up(tmp_path)
        assert len(remaining) == 3
        # The 3 most-recently-added ids survive; the 2 oldest are evicted.
        assert remaining == {"t::2", "t::3", "t::4"}

    def test_rerecord_refreshes_recency(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bfm, "MAX_ENTRIES", 3)
        for i in range(3):
            bfm.record_given_up(tmp_path, [f"t::{i}"], attempts=1)
        # Touch the oldest so it becomes most-recent.
        bfm.record_given_up(tmp_path, ["t::0"], attempts=1)
        # Add a new id, which should evict t::1 (now oldest), not t::0.
        bfm.record_given_up(tmp_path, ["t::3"], attempts=1)
        remaining = bfm.load_given_up(tmp_path)
        assert remaining == {"t::0", "t::2", "t::3"}
