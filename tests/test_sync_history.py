"""Tests for SyncFlowContext — flow_id generation, step_id formatting, _meta.json."""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from se3.engine.sync_engine import (
    DiffType,
    LoopResult,
    RoundResult,
    SpecAnalysis,
    SpecDiff,
    SyncResult,
)
from se3.engine.sync_history import SyncFlowContext


FLOW_ID_RE = re.compile(r"^\d{8}-\d{6}_[0-9a-f]{8}$")


class TestFlowIdGeneration:
    def test_auto_generated_format(self):
        ctx = SyncFlowContext(Path("/tmp/fake"))
        assert FLOW_ID_RE.match(ctx.flow_id), f"flow_id '{ctx.flow_id}' doesn't match expected format"

    def test_explicit_flow_id(self):
        ctx = SyncFlowContext(Path("/tmp/fake"), flow_id="20260415-120000_aabbccdd")
        assert ctx.flow_id == "20260415-120000_aabbccdd"

    def test_unique_flow_ids(self):
        ids = {SyncFlowContext(Path("/tmp/fake")).flow_id for _ in range(10)}
        assert len(ids) == 10


class TestMakeStepId:
    def test_with_suffix(self):
        ctx = SyncFlowContext(Path("/tmp/fake"))
        assert ctx.make_step_id("sync_analyze", "flow-engine") == "sync_analyze_flow-engine"

    def test_auto_increment_without_suffix(self):
        ctx = SyncFlowContext(Path("/tmp/fake"))
        assert ctx.make_step_id("sync_scan") == "sync_scan_0"
        assert ctx.make_step_id("sync_scan") == "sync_scan_1"
        assert ctx.make_step_id("sync_scan") == "sync_scan_2"

    def test_separate_counters_per_step_type(self):
        ctx = SyncFlowContext(Path("/tmp/fake"))
        assert ctx.make_step_id("sync_scan") == "sync_scan_0"
        assert ctx.make_step_id("sync_resolve") == "sync_resolve_0"
        assert ctx.make_step_id("sync_scan") == "sync_scan_1"

    def test_suffix_does_not_affect_counter(self):
        ctx = SyncFlowContext(Path("/tmp/fake"))
        assert ctx.make_step_id("sync_analyze", "base") == "sync_analyze_base"
        assert ctx.make_step_id("sync_analyze") == "sync_analyze_0"


class TestWriteMeta:
    def test_creates_meta_json(self, tmp_path):
        ctx = SyncFlowContext(tmp_path, flow_id="20260415-120000_aabbccdd")
        meta_path = ctx.write_meta()

        assert meta_path.exists()
        assert meta_path.parent == tmp_path / "se3" / "history" / "20260415-120000_aabbccdd"

        data = json.loads(meta_path.read_text(encoding="utf-8"))
        assert "se3_version" in data
        assert "python_version" in data
        assert "created_at" in data
        assert data["type"] == "sync"

    def test_idempotent_write(self, tmp_path):
        ctx = SyncFlowContext(tmp_path, flow_id="20260415-120000_aabbccdd")
        path1 = ctx.write_meta()
        content1 = path1.read_text(encoding="utf-8")

        path2 = ctx.write_meta()
        content2 = path2.read_text(encoding="utf-8")

        assert content1 == content2

    def test_creates_directory_structure(self, tmp_path):
        ctx = SyncFlowContext(tmp_path)
        ctx.write_meta()
        history_dir = tmp_path / "se3" / "history" / ctx.flow_id
        assert history_dir.is_dir()
        assert (history_dir / "_meta.json").is_file()


class TestSyncLoopHistoryIntegration:
    """Verify that ``SyncLoop`` creates flow context and writes rounds summary.

    The engine itself is stateless across rounds; ``SyncFlowContext`` /
    ``_rounds.json`` recording lives in the loop layer.
    """

    def test_loop_writes_rounds_summary(self, tmp_path):
        rr = RoundResult(round_index=1)
        rr.specs_updated = 1
        rr.changes_by_spec = {"auth": ["added: helper"]}
        rr.duration_seconds = 0.5

        loop = LoopResult(
            rounds=[rr],
            converged=True,
            total_specs_updated=1,
            final_round_index=1,
        )
        ctx = SyncFlowContext(tmp_path, flow_id="20260415-120000_aabbccdd")
        out_path = ctx.write_rounds_summary(loop)

        assert out_path.exists()
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert data["converged"] is True
        assert data["total_specs_updated"] == 1
        assert data["rounds"][0]["round_index"] == 1
        assert data["rounds"][0]["changes_by_spec"] == {"auth": ["added: helper"]}


class TestRoundAwareStepId:
    """make_round_step_id namespaces step ids by round."""

    def test_round_step_id_includes_round_index(self):
        ctx = SyncFlowContext(Path("/tmp/fake"))
        sid = ctx.make_round_step_id(2, "analyze", "auth")
        assert sid == "sync_analyze_r2_auth"

    def test_round_step_id_strips_sync_prefix(self):
        ctx = SyncFlowContext(Path("/tmp/fake"))
        sid = ctx.make_round_step_id(1, "sync_analyze", "auth")
        assert sid == "sync_analyze_r1_auth"

    def test_round_step_id_no_suffix_uses_counter(self):
        ctx = SyncFlowContext(Path("/tmp/fake"))
        a = ctx.make_round_step_id(1, "scan")
        b = ctx.make_round_step_id(1, "scan")
        assert a == "sync_scan_r1_0"
        assert b == "sync_scan_r1_1"
