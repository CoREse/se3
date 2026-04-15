"""Tests for SyncFlowContext — flow_id generation, step_id formatting, _meta.json."""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from se3.engine.sync_engine import (
    DiffType,
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


class TestSyncEngineHistoryIntegration:
    """Verify that SyncEngine.run() creates flow context and passes flow_id to LLMCaller."""

    @patch("se3.engine.sync_engine.SyncEngine._load_specs")
    @patch("se3.engine.sync_engine.SyncEngine._load_existing_issues")
    def test_run_creates_history_directory(self, mock_issues, mock_specs, tmp_path):
        from se3.engine.sync_engine import SyncEngine

        mock_specs.return_value = {}
        mock_issues.return_value = []

        with patch("se3.engine.llm_caller.LLMCaller.call") as mock_call, \
             patch("se3.engine.sync_discovery.SpecDiscovery.discover_missing_specs", return_value=[]):
            mock_call.return_value = '{"diffs": []}'

            engine = SyncEngine(tmp_path, mode="fast")
            engine.run()

        history_dir = tmp_path / "se3" / "history"
        assert history_dir.exists()
        flow_dirs = list(history_dir.iterdir())
        assert len(flow_dirs) == 1
        assert (flow_dirs[0] / "_meta.json").exists()

    @patch("se3.engine.sync_engine.SyncEngine._load_specs")
    @patch("se3.engine.sync_engine.SyncEngine._load_existing_issues")
    def test_llm_caller_receives_flow_id(self, mock_issues, mock_specs, tmp_path):
        from se3.engine.sync_engine import SyncEngine

        mock_specs.return_value = {"base": {"name": "base", "path": tmp_path / "s.md", "content": "# Base"}}
        mock_issues.return_value = []

        captured_callers = []
        original_init = None

        from se3.engine.llm_caller import LLMCaller
        original_init = LLMCaller.__init__

        def spy_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            captured_callers.append(self)

        with patch.object(LLMCaller, "__init__", spy_init), \
             patch.object(LLMCaller, "call", return_value='{"diffs": []}'), \
             patch("se3.engine.sync_discovery.SpecDiscovery.discover_missing_specs", return_value=[]):
            engine = SyncEngine(tmp_path, mode="fast")
            engine.run()

        assert len(captured_callers) >= 1
        caller = captured_callers[0]
        assert caller.flow_id is not None
        assert FLOW_ID_RE.match(caller.flow_id)

    @patch("se3.engine.sync_engine.SyncEngine._load_specs")
    @patch("se3.engine.sync_engine.SyncEngine._load_existing_issues")
    def test_step_id_set_for_analysis(self, mock_issues, mock_specs, tmp_path):
        from se3.engine.sync_engine import SyncEngine

        spec_path = tmp_path / "se3" / "specs" / "auth" / "spec.md"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text("# Auth spec", encoding="utf-8")

        mock_specs.return_value = {
            "auth": {"name": "auth", "path": spec_path, "content": "# Auth spec"}
        }
        mock_issues.return_value = []

        step_ids_seen = []
        from se3.engine.llm_caller import LLMCaller
        original_call = LLMCaller.call

        def spy_call(self, *args, **kwargs):
            step_ids_seen.append(self.step_id)
            return '{"diffs": []}'

        with patch.object(LLMCaller, "call", spy_call), \
             patch("se3.engine.sync_discovery.SpecDiscovery.discover_missing_specs", return_value=[]):
            engine = SyncEngine(tmp_path, mode="fast")
            engine.run()

        assert any(sid == "sync_analyze_auth" for sid in step_ids_seen), \
            f"Expected 'sync_analyze_auth' in {step_ids_seen}"

    @patch("se3.engine.sync_engine.SyncEngine._load_specs")
    @patch("se3.engine.sync_engine.SyncEngine._load_existing_issues")
    def test_step_id_set_for_discovery(self, mock_issues, mock_specs, tmp_path):
        from se3.engine.sync_engine import SyncEngine

        mock_specs.return_value = {"base": {"name": "base", "path": tmp_path / "s.md", "content": "# Base"}}
        mock_issues.return_value = []

        step_ids_at_discover = []
        from se3.engine.llm_caller import LLMCaller
        from se3.engine.sync_discovery import SpecDiscovery

        original_discover = SpecDiscovery.discover_missing_specs

        def spy_discover(self_disc, *args, **kwargs):
            step_ids_at_discover.append(self_disc.llm_caller.step_id)
            return []

        with patch.object(LLMCaller, "call", return_value='{"diffs": []}'), \
             patch.object(SpecDiscovery, "discover_missing_specs", spy_discover):
            engine = SyncEngine(tmp_path, mode="fast")
            engine.run()

        assert len(step_ids_at_discover) == 1
        assert step_ids_at_discover[0].startswith("sync_scan"), \
            f"Expected step_id starting with 'sync_scan', got '{step_ids_at_discover[0]}'"


class TestSyncModeEnum:
    def test_has_three_values(self):
        from se3.commands.sync import SyncMode
        assert SyncMode.DEFAULT.value == "default"
        assert SyncMode.STRICT.value == "strict"
        assert SyncMode.FAST.value == "fast"
        assert len(SyncMode) == 3

    def test_string_construction(self):
        from se3.commands.sync import SyncMode
        assert SyncMode("default") == SyncMode.DEFAULT
        assert SyncMode("strict") == SyncMode.STRICT
        assert SyncMode("fast") == SyncMode.FAST


class TestRenderSyncResultsExpanded:
    """Test the expanded _render_sync_results with new fields."""

    def test_render_with_detailed_changes(self):
        from se3.commands.sync import _render_sync_results

        result = SyncResult(
            analyses=[SpecAnalysis(spec_name="auth", diffs=[
                SpecDiff(DiffType.EXTENSION, "auth", "Extra feature"),
            ])],
            specs_updated=1,
            detailed_changes=[
                {"spec_name": "auth", "action": "updated", "description": "Added extra feature"},
            ],
        )
        _render_sync_results(result)

    def test_render_with_specs_created(self):
        from se3.commands.sync import _render_sync_results

        result = SyncResult(
            analyses=[],
            specs_created=["cli-tools", "data-pipeline"],
        )
        _render_sync_results(result)

    def test_render_with_gap_resolutions(self):
        from se3.commands.sync import _render_sync_results

        result = SyncResult(
            analyses=[SpecAnalysis(spec_name="auth", diffs=[
                SpecDiff(DiffType.GAP, "auth", "Old requirement"),
            ])],
            gap_resolutions=[
                {"spec_name": "auth", "action": "update_spec", "description": "Old requirement"},
                {"spec_name": "base", "action": "create_issue", "description": "Missing feature"},
            ],
            issues_created=1,
        )
        _render_sync_results(result)

    def test_render_with_all_new_fields(self):
        from se3.commands.sync import _render_sync_results

        result = SyncResult(
            analyses=[
                SpecAnalysis(spec_name="auth", diffs=[
                    SpecDiff(DiffType.GAP, "auth", "Old requirement"),
                    SpecDiff(DiffType.EXTENSION, "auth", "New helper"),
                ]),
            ],
            issues_created=1,
            specs_updated=2,
            specs_created=["new-module"],
            gap_resolutions=[
                {"spec_name": "auth", "action": "update_spec", "description": "Old requirement"},
            ],
            detailed_changes=[
                {"spec_name": "auth", "action": "updated", "description": "Added new helper"},
            ],
        )
        _render_sync_results(result)

    def test_render_preserves_original_table(self):
        from io import StringIO
        from rich.console import Console
        from se3.commands.sync import _render_sync_results

        result = SyncResult(
            analyses=[SpecAnalysis(spec_name="base", diffs=[])],
        )

        console = Console(file=StringIO(), force_terminal=True)
        with patch("se3.commands.sync.get_console", return_value=console):
            _render_sync_results(result)

        output = console.file.getvalue()
        assert "Spec Status Overview" in output
        assert "base" in output

    def test_render_specs_created_in_summary(self):
        from io import StringIO
        from rich.console import Console
        from se3.commands.sync import _render_sync_results

        result = SyncResult(
            analyses=[],
            specs_created=["new-spec-1", "new-spec-2"],
        )

        console = Console(file=StringIO(), force_terminal=True)
        with patch("se3.commands.sync.get_console", return_value=console):
            _render_sync_results(result)

        output = console.file.getvalue()
        assert "New specs" in output or "new-spec-1" in output
