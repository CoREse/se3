"""Unit tests for SpecDiscovery — directory scanning, spec summarization, LLM discovery, spec generation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from se3.engine.sync_discovery import SpecDiscovery, _EXCLUDED_DIRS


# ---------------------------------------------------------------------------
# Helper to create a SpecDiscovery with a mock LLM
# ---------------------------------------------------------------------------

def _make_discovery(tmp_path, llm_response=None):
    llm = MagicMock()
    if llm_response is not None:
        llm.call.return_value = llm_response
    return SpecDiscovery(tmp_path, llm), llm


def _create_spec(tmp_path, name, content="# Spec\n## Purpose\nTest spec.\n## Requirements\n### Requirement: Feature A\nDetails."):
    spec_dir = tmp_path / "se3" / "specs" / name
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text(content, encoding="utf-8")
    return {
        "name": name,
        "path": spec_dir / "spec.md",
        "content": content,
    }


def _create_source_files(tmp_path):
    """Create a minimal source tree for directory scanning."""
    src = tmp_path / "src" / "myapp"
    src.mkdir(parents=True, exist_ok=True)
    (src / "__init__.py").write_text("")
    (src / "main.py").write_text("def main(): pass")
    (src / "utils.py").write_text("def helper(): pass")
    tests = tmp_path / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "test_main.py").write_text("def test_main(): pass")
    return tmp_path


# ---------------------------------------------------------------------------
# Directory tree generation
# ---------------------------------------------------------------------------

class TestGetDirectoryTree:
    def test_excludes_git_dir(self, tmp_path):
        (tmp_path / ".git" / "objects").mkdir(parents=True)
        (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("code")

        discovery, _ = _make_discovery(tmp_path)
        tree = discovery._tree_from_walk(tmp_path)

        assert ".git" not in tree
        assert "src" in tree

    def test_excludes_node_modules(self, tmp_path):
        (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
        (tmp_path / "node_modules" / "pkg" / "index.js").write_text("mod")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("code")

        discovery, _ = _make_discovery(tmp_path)
        tree = discovery._tree_from_walk(tmp_path)

        assert "node_modules" not in tree

    def test_excludes_pycache(self, tmp_path):
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "mod.cpython-311.pyc").write_text("bytes")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("code")

        discovery, _ = _make_discovery(tmp_path)
        tree = discovery._tree_from_walk(tmp_path)

        assert "__pycache__" not in tree

    def test_excludes_se3_runtime(self, tmp_path):
        (tmp_path / "se3" / "state").mkdir(parents=True)
        (tmp_path / "se3" / "state" / "engine.json").write_text("{}")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("code")

        discovery, _ = _make_discovery(tmp_path)
        tree = discovery._tree_from_walk(tmp_path)

        assert "se3" not in tree

    def test_excludes_venv(self, tmp_path):
        (tmp_path / ".venv" / "lib").mkdir(parents=True)
        (tmp_path / ".venv" / "lib" / "site.py").write_text("code")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("code")

        discovery, _ = _make_discovery(tmp_path)
        tree = discovery._tree_from_walk(tmp_path)

        assert ".venv" not in tree

    def test_includes_source_dirs(self, tmp_path):
        _create_source_files(tmp_path)

        discovery, _ = _make_discovery(tmp_path)
        tree = discovery._tree_from_walk(tmp_path)

        assert "src" in tree
        assert "main.py" in tree

    def test_git_ls_files_excludes_dirs(self, tmp_path):
        discovery, _ = _make_discovery(tmp_path)
        files = [
            "src/app.py",
            "src/utils.py",
            "__pycache__/mod.pyc",
            "se3/state/engine.json",
            ".git/HEAD",
            "node_modules/pkg/index.js",
        ]

        tree = discovery._tree_from_git_files(files)

        assert "app.py" in tree
        assert "utils.py" in tree
        assert "__pycache__" not in tree
        assert "se3" not in tree
        assert ".git" not in tree
        assert "node_modules" not in tree


# ---------------------------------------------------------------------------
# Spec summary building
# ---------------------------------------------------------------------------

class TestBuildSpecsSummary:
    def test_empty_specs(self, tmp_path):
        discovery, _ = _make_discovery(tmp_path)
        summary = discovery._build_specs_summary({})

        assert "(no existing specs)" in summary

    def test_includes_spec_name(self, tmp_path):
        discovery, _ = _make_discovery(tmp_path)
        specs = {"auth": _create_spec(tmp_path, "auth")}

        summary = discovery._build_specs_summary(specs)

        assert "### auth" in summary

    def test_includes_purpose(self, tmp_path):
        discovery, _ = _make_discovery(tmp_path)
        content = "# auth Specification\n## Purpose\nHandles authentication.\n## Requirements\n### Requirement: Login\nLogin flow."
        specs = {"auth": {"name": "auth", "content": content}}

        summary = discovery._build_specs_summary(specs)

        assert "Handles authentication" in summary

    def test_includes_requirement_titles(self, tmp_path):
        discovery, _ = _make_discovery(tmp_path)
        content = "# Spec\n## Purpose\nPurpose.\n## Requirements\n### Requirement: User Login\nDetails.\n### Requirement: Token Refresh\nMore."
        specs = {"auth": {"name": "auth", "content": content}}

        summary = discovery._build_specs_summary(specs)

        assert "User Login" in summary
        assert "Token Refresh" in summary

    def test_truncates_to_summary_not_full_content(self, tmp_path):
        discovery, _ = _make_discovery(tmp_path)
        long_scenario = "#### Scenario: Test\n- **WHEN** something\n- **THEN** result\n" * 50
        content = f"# Spec\n## Purpose\nShort purpose.\n## Requirements\n### Requirement: Feature\n{long_scenario}"
        specs = {"big": {"name": "big", "content": content}}

        summary = discovery._build_specs_summary(specs)

        assert "Short purpose" in summary
        assert "WHEN" not in summary


class TestExtractSpecSummary:
    def test_extracts_purpose_paragraph(self, tmp_path):
        discovery, _ = _make_discovery(tmp_path)
        content = "# Spec\n## Purpose\nThis is the purpose paragraph.\n## Requirements\n"

        summary = discovery._extract_spec_summary("test", content)

        assert "This is the purpose paragraph" in summary

    def test_extracts_multi_line_purpose(self, tmp_path):
        discovery, _ = _make_discovery(tmp_path)
        content = "# Spec\n## Purpose\nFirst line.\nSecond line.\n## Requirements\n"

        summary = discovery._extract_spec_summary("test", content)

        assert "First line." in summary
        assert "Second line." in summary

    def test_extracts_requirement_headings(self, tmp_path):
        discovery, _ = _make_discovery(tmp_path)
        content = "# Spec\n## Purpose\nPurpose.\n## Requirements\n### Requirement: Alpha\nDetails.\n### Requirement: Beta\nMore."

        summary = discovery._extract_spec_summary("test", content)

        assert "Alpha" in summary
        assert "Beta" in summary

    def test_no_purpose_section(self, tmp_path):
        discovery, _ = _make_discovery(tmp_path)
        content = "# Spec\n## Requirements\n### Requirement: Only\nDetails."

        summary = discovery._extract_spec_summary("test", content)

        assert "### test" in summary
        assert "Only" in summary


# ---------------------------------------------------------------------------
# discover_missing_specs
# ---------------------------------------------------------------------------

class TestDiscoverMissingSpecs:
    def test_returns_discovered_subsystems(self, tmp_path):
        _create_source_files(tmp_path)
        subsystems = [
            {"name": "data-pipeline", "description": "Handles data processing", "relevant_files": ["src/myapp/main.py"]},
        ]
        discovery, llm = _make_discovery(tmp_path, json.dumps(subsystems))

        result = discovery.discover_missing_specs({"base": _create_spec(tmp_path, "base")})

        assert len(result) == 1
        assert result[0]["name"] == "data-pipeline"
        assert result[0]["description"] == "Handles data processing"
        assert result[0]["relevant_files"] == ["src/myapp/main.py"]

    def test_returns_empty_when_llm_returns_empty_array(self, tmp_path):
        discovery, llm = _make_discovery(tmp_path, "[]")

        result = discovery.discover_missing_specs({})

        assert result == []

    def test_returns_empty_on_llm_failure(self, tmp_path):
        discovery, llm = _make_discovery(tmp_path)
        llm.call.side_effect = RuntimeError("LLM error")

        result = discovery.discover_missing_specs({})

        assert result == []

    def test_filters_invalid_entries(self, tmp_path):
        subsystems = [
            {"name": "valid", "description": "Good entry", "relevant_files": []},
            {"name": "", "description": "Missing name"},
            {"description": "No name key"},
            "not a dict",
            {"name": "no-desc", "description": ""},
        ]
        discovery, llm = _make_discovery(tmp_path, json.dumps(subsystems))

        result = discovery.discover_missing_specs({})

        assert len(result) == 1
        assert result[0]["name"] == "valid"

    def test_returns_empty_on_non_list_response(self, tmp_path):
        discovery, llm = _make_discovery(tmp_path, '{"not": "a list"}')

        result = discovery.discover_missing_specs({})

        assert result == []

    def test_llm_prompt_contains_directory_structure(self, tmp_path):
        _create_source_files(tmp_path)
        discovery, llm = _make_discovery(tmp_path, "[]")

        with patch.object(discovery, '_get_directory_tree', return_value="src/\n  app.py") as mock_tree:
            discovery.discover_missing_specs({"base": _create_spec(tmp_path, "base")})

        prompt = llm.call.call_args[1].get("prompt") or llm.call.call_args[0][0]
        assert "src/" in prompt or mock_tree.called

    def test_llm_prompt_contains_spec_summary(self, tmp_path):
        content = "# Spec\n## Purpose\nAuth handling.\n## Requirements\n### Requirement: Login\nFlow."
        specs = {"auth": {"name": "auth", "content": content}}
        discovery, llm = _make_discovery(tmp_path, "[]")

        discovery.discover_missing_specs(specs)

        prompt = llm.call.call_args[1].get("prompt") or llm.call.call_args[0][0]
        assert "auth" in prompt.lower()
        assert "Auth handling" in prompt

    def test_uses_extract_json_mode(self, tmp_path):
        discovery, llm = _make_discovery(tmp_path, "[]")

        discovery.discover_missing_specs({})

        llm.call.assert_called_once()
        assert llm.call.call_args[1].get("json_mode") == "extract"

    def test_handles_missing_relevant_files_key(self, tmp_path):
        subsystems = [
            {"name": "no-files", "description": "Has no relevant_files key"},
        ]
        discovery, llm = _make_discovery(tmp_path, json.dumps(subsystems))

        result = discovery.discover_missing_specs({})

        assert len(result) == 1
        assert result[0]["relevant_files"] == []


# ---------------------------------------------------------------------------
# generate_spec_for_subsystem
# ---------------------------------------------------------------------------

class TestGenerateSpecForSubsystem:
    def test_creates_spec_file(self, tmp_path):
        spec_content = "# data-pipeline Specification\n\n## Purpose\n\nHandles data processing.\n\n## Requirements\n\n### Requirement: Pipeline Execution\nRuns data pipeline."
        discovery, llm = _make_discovery(tmp_path, spec_content)

        subsystem = {"name": "data-pipeline", "description": "Handles data processing", "relevant_files": ["src/pipeline.py"]}
        result = discovery.generate_spec_for_subsystem(subsystem)

        assert result is not None
        assert result.exists()
        assert result.name == "spec.md"
        assert result.parent.name == "data-pipeline"

        content = result.read_text(encoding="utf-8")
        assert "data-pipeline Specification" in content
        assert "Purpose" in content

    def test_creates_spec_directory(self, tmp_path):
        spec_content = "# new-feature Specification\n\n## Purpose\n\nNew feature.\n\n## Requirements\n\n### Requirement: Feature\nDetails."
        discovery, llm = _make_discovery(tmp_path, spec_content)

        subsystem = {"name": "new-feature", "description": "New feature", "relevant_files": []}
        result = discovery.generate_spec_for_subsystem(subsystem)

        assert (tmp_path / "se3" / "specs" / "new-feature").is_dir()
        assert (tmp_path / "se3" / "specs" / "new-feature" / "spec.md").exists()

    def test_returns_none_on_empty_response(self, tmp_path):
        discovery, llm = _make_discovery(tmp_path, "")

        subsystem = {"name": "empty", "description": "Empty", "relevant_files": []}
        result = discovery.generate_spec_for_subsystem(subsystem)

        assert result is None

    def test_returns_none_on_short_response(self, tmp_path):
        discovery, llm = _make_discovery(tmp_path, "Too short")

        subsystem = {"name": "short", "description": "Short", "relevant_files": []}
        result = discovery.generate_spec_for_subsystem(subsystem)

        assert result is None

    def test_returns_none_on_llm_failure(self, tmp_path):
        discovery, llm = _make_discovery(tmp_path)
        llm.call.side_effect = RuntimeError("LLM error")

        subsystem = {"name": "fail", "description": "Fail", "relevant_files": []}
        result = discovery.generate_spec_for_subsystem(subsystem)

        assert result is None

    def test_strips_markdown_fences(self, tmp_path):
        fenced = "```markdown\n# my-spec Specification\n\n## Purpose\n\nDoes things.\n\n## Requirements\n\n### Requirement: Thing\nDetails about the thing.\n```"
        discovery, llm = _make_discovery(tmp_path, fenced)

        subsystem = {"name": "my-spec", "description": "Does things", "relevant_files": []}
        result = discovery.generate_spec_for_subsystem(subsystem)

        assert result is not None
        content = result.read_text(encoding="utf-8")
        assert not content.startswith("```")
        assert "my-spec Specification" in content

    def test_uses_off_json_mode(self, tmp_path):
        spec_content = "# test Specification\n\n## Purpose\n\nTest.\n\n## Requirements\n\n### Requirement: Test\nDetails."
        discovery, llm = _make_discovery(tmp_path, spec_content)

        subsystem = {"name": "test", "description": "Test", "relevant_files": []}
        discovery.generate_spec_for_subsystem(subsystem)

        llm.call.assert_called_once()
        assert llm.call.call_args[1].get("json_mode") == "off"

    def test_prompt_contains_subsystem_info(self, tmp_path):
        spec_content = "# sub Specification\n\n## Purpose\n\nSub.\n\n## Requirements\n\n### Requirement: Sub\nDetails."
        discovery, llm = _make_discovery(tmp_path, spec_content)

        subsystem = {"name": "sub", "description": "Subsystem desc", "relevant_files": ["src/a.py", "src/b.py"]}
        discovery.generate_spec_for_subsystem(subsystem)

        prompt = llm.call.call_args[1].get("prompt") or llm.call.call_args[0][0]
        assert "sub" in prompt
        assert "Subsystem desc" in prompt
        assert "src/a.py" in prompt
        assert "src/b.py" in prompt


# ---------------------------------------------------------------------------
# SyncEngine integration
# ---------------------------------------------------------------------------

class TestSyncEngineIntegration:
    def test_new_specs_added_to_sync_flow(self, tmp_path):
        """New specs from discovery should be included in the specs dict for sync."""
        from se3.engine.sync_engine import SyncEngine, SpecAnalysis

        _create_spec(tmp_path, "base")

        discovered = [
            {"name": "new-feature", "description": "New feature", "relevant_files": ["src/feat.py"]},
        ]
        spec_content = "# new-feature Specification\n\n## Purpose\n\nNew feature.\n\n## Requirements\n\n### Requirement: Feature\nDetails."

        with patch("se3.engine.sync_engine.SyncEngine._load_existing_issues", return_value=[]), \
             patch("se3.engine.llm_caller.LLMCaller.__init__", return_value=None), \
             patch("se3.engine.project_context.ProjectContextCollector.collect",
                   return_value={"git": {}, "flow_engine": None, "backlog": [], "specs": []}), \
             patch("se3.engine.sync_analyzer.SyncAnalyzer.analyze_spec") as mock_analyze, \
             patch("se3.engine.sync_discovery.SpecDiscovery.discover_missing_specs") as mock_discover, \
             patch("se3.engine.sync_discovery.SpecDiscovery.generate_spec_for_subsystem") as mock_gen:

            mock_discover.return_value = discovered
            spec_path = tmp_path / "se3" / "specs" / "new-feature" / "spec.md"
            spec_path.parent.mkdir(parents=True, exist_ok=True)
            spec_path.write_text(spec_content, encoding="utf-8")
            mock_gen.return_value = spec_path
            mock_analyze.return_value = SpecAnalysis(spec_name="base")

            engine = SyncEngine(tmp_path)
            result = engine.run()

        assert "new-feature" in result.specs_created

    def test_specs_created_recorded_in_result(self, tmp_path):
        """SyncResult.specs_created should list newly discovered specs."""
        from se3.engine.sync_engine import SyncEngine, SpecAnalysis

        _create_spec(tmp_path, "base")

        discovered = [
            {"name": "sub-a", "description": "Subsystem A", "relevant_files": []},
            {"name": "sub-b", "description": "Subsystem B", "relevant_files": []},
        ]

        spec_a_content = "# sub-a Specification\n\n## Purpose\n\nSub A.\n\n## Requirements\n\n### Requirement: A\nDetails."
        spec_b_content = "# sub-b Specification\n\n## Purpose\n\nSub B.\n\n## Requirements\n\n### Requirement: B\nDetails."

        with patch("se3.engine.sync_engine.SyncEngine._load_existing_issues", return_value=[]), \
             patch("se3.engine.llm_caller.LLMCaller.__init__", return_value=None), \
             patch("se3.engine.project_context.ProjectContextCollector.collect",
                   return_value={"git": {}, "flow_engine": None, "backlog": [], "specs": []}), \
             patch("se3.engine.sync_analyzer.SyncAnalyzer.analyze_spec") as mock_analyze, \
             patch("se3.engine.sync_discovery.SpecDiscovery.discover_missing_specs") as mock_discover, \
             patch("se3.engine.sync_discovery.SpecDiscovery.generate_spec_for_subsystem") as mock_gen:

            mock_discover.return_value = discovered

            def gen_side_effect(subsystem):
                name = subsystem["name"]
                content = spec_a_content if name == "sub-a" else spec_b_content
                spec_path = tmp_path / "se3" / "specs" / name / "spec.md"
                spec_path.parent.mkdir(parents=True, exist_ok=True)
                spec_path.write_text(content, encoding="utf-8")
                return spec_path

            mock_gen.side_effect = gen_side_effect
            mock_analyze.return_value = SpecAnalysis(spec_name="x")

            engine = SyncEngine(tmp_path)
            result = engine.run()

        assert "sub-a" in result.specs_created
        assert "sub-b" in result.specs_created

    def test_progress_callback_reports_discovering_phase(self, tmp_path):
        """progress_callback should be called with phase='discovering'."""
        from se3.engine.sync_engine import SyncEngine, SpecAnalysis

        _create_spec(tmp_path, "base")

        callback = MagicMock()

        with patch("se3.engine.sync_engine.SyncEngine._load_existing_issues", return_value=[]), \
             patch("se3.engine.llm_caller.LLMCaller.__init__", return_value=None), \
             patch("se3.engine.project_context.ProjectContextCollector.collect",
                   return_value={"git": {}, "flow_engine": None, "backlog": [], "specs": []}), \
             patch("se3.engine.sync_analyzer.SyncAnalyzer.analyze_spec") as mock_analyze, \
             patch("se3.engine.sync_discovery.SpecDiscovery.discover_missing_specs", return_value=[]):

            mock_analyze.return_value = SpecAnalysis(spec_name="base")

            engine = SyncEngine(tmp_path)
            engine.run(progress_callback=callback)

        discovering_calls = [
            c for c in callback.call_args_list
            if c[0][0] == "discovering"
        ]
        assert len(discovering_calls) >= 1

    def test_discovery_runs_before_analysis(self, tmp_path):
        """SpecDiscovery should execute before per-spec analysis."""
        from se3.engine.sync_engine import SyncEngine, SpecAnalysis

        _create_spec(tmp_path, "base")
        call_order = []

        with patch("se3.engine.sync_engine.SyncEngine._load_existing_issues", return_value=[]), \
             patch("se3.engine.llm_caller.LLMCaller.__init__", return_value=None), \
             patch("se3.engine.project_context.ProjectContextCollector.collect",
                   return_value={"git": {}, "flow_engine": None, "backlog": [], "specs": []}), \
             patch("se3.engine.sync_analyzer.SyncAnalyzer.analyze_spec") as mock_analyze, \
             patch("se3.engine.sync_discovery.SpecDiscovery.discover_missing_specs") as mock_discover:

            def discover_side(*a, **kw):
                call_order.append("discover")
                return []
            mock_discover.side_effect = discover_side

            def analyze_side(*a, **kw):
                call_order.append("analyze")
                return SpecAnalysis(spec_name="base")
            mock_analyze.side_effect = analyze_side

            engine = SyncEngine(tmp_path)
            engine.run()

        assert call_order.index("discover") < call_order.index("analyze")

    def test_discovery_failure_does_not_block_sync(self, tmp_path):
        """If discovery fails, sync should continue with existing specs."""
        from se3.engine.sync_engine import SyncEngine, SpecAnalysis

        _create_spec(tmp_path, "base")

        with patch("se3.engine.sync_engine.SyncEngine._load_existing_issues", return_value=[]), \
             patch("se3.engine.llm_caller.LLMCaller.__init__", return_value=None), \
             patch("se3.engine.project_context.ProjectContextCollector.collect",
                   return_value={"git": {}, "flow_engine": None, "backlog": [], "specs": []}), \
             patch("se3.engine.sync_analyzer.SyncAnalyzer.analyze_spec") as mock_analyze, \
             patch("se3.engine.sync_discovery.SpecDiscovery.discover_missing_specs") as mock_discover:

            mock_discover.side_effect = RuntimeError("LLM down")
            mock_analyze.return_value = SpecAnalysis(spec_name="base")

            engine = SyncEngine(tmp_path)
            result = engine.run()

        assert result.specs_created == []
        assert len(result.analyses) >= 1
