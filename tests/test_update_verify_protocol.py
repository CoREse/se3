"""Tests for the G7 index-first spec protocol in update_spec / verify_spec.

Covers:
- update_spec prompt carries the index-first (`se3 spec index` -> `se3 spec show`)
  protocol, the directed Read+Edit instruction for existing Requirements, and an
  explicit prohibition on reading the whole spec / the index cache file.
- update_spec injects the base Spec Admission Standard with the size-prevention
  directive (route over-limit content into the module spec, not base).
- update_spec's New Spec Decision consumes the root index view (rendered by the
  same renderer as `se3 spec index`) instead of a plain spec-names list, and
  instructs writing the `<!-- domain: ... -->` header on a new spec.
- verify_spec prompt carries the same index-first protocol and prohibition.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from se3.engine.models import FlowInstance, FlowStatus, Step, StepStatus, StepType
from se3.engine.spec_governance import BASE_ADMISSION_STANDARD
from se3.engine.steps.update_spec import (
    UPDATE_SPEC_PROMPT,
    _build_root_view_injection,
    update_spec_handler,
)
from se3.engine.steps.verify_spec import VERIFY_PROMPT, verify_spec_handler


# ---------------------------------------------------------------------------
# Static template assertions (no LLM, no handler)
# ---------------------------------------------------------------------------

class TestUpdateSpecPromptProtocol:
    """The static UPDATE_SPEC_PROMPT carries the index-first protocol."""

    def _render(self) -> str:
        return UPDATE_SPEC_PROMPT.format(
            task_description="t",
            changes_made="c",
            verification_result="v",
            spec_changes="s",
            design_doc="d",
            specs_dir="/x",
            redo_guidance="",
            base_admission_standard=BASE_ADMISSION_STANDARD,
        )

    def test_index_first_commands_present(self):
        p = self._render()
        assert "se3 spec index" in p
        assert "se3 spec show <spec>::<requirement>" in p

    def test_forbids_reading_whole_spec_and_index_cache(self):
        p = self._render()
        assert "se3/cache/spec-index.json" in p
        # The prohibition wording must reject whole-file reads for context.
        assert "MUST NOT" in p
        assert "whole spec" in p.lower() or "entire large spec" in p.lower()

    def test_directed_read_edit_for_existing_requirement(self):
        p = self._render()
        assert "Directed edit" in p
        assert "se3 spec show <spec>::<requirement>" in p
        # Localized edit on the physical line range, not a whole-file read.
        assert "line range" in p.lower()
        assert "directed read+edit" in p.lower()

    def test_base_admission_standard_injected(self):
        p = self._render()
        assert "base Spec Admission Standard" in p
        # Size-prevention directive: route over-limit content into the module spec.
        assert "over its size limit" in p
        assert "module spec" in p

    def test_new_spec_decision_consumes_root_view(self):
        p = self._render()
        assert "root index view" in p.lower()
        assert "New Spec Decision" in p

    def test_new_spec_creation_writes_domain_marker(self):
        p = self._render()
        assert "<!-- domain:" in p
        assert "<!-- spec-format: v1 -->" in p


class TestVerifySpecPromptProtocol:
    """The static VERIFY_PROMPT carries the index-first protocol."""

    def _render(self) -> str:
        return VERIFY_PROMPT.format(
            task_description="t",
            spec_content="s",
            changes_made="c",
            spec_changes="sc",
            test_results="tr",
            fix_context="fc",
            previous_verification="",
        )

    def test_index_first_commands_present(self):
        v = self._render()
        assert "se3 spec index" in v
        assert "se3 spec show <spec>::<requirement>" in v

    def test_forbids_reading_whole_spec_and_index_cache(self):
        v = self._render()
        assert "se3/cache/spec-index.json" in v
        assert "MUST NOT" in v

    def test_read_only_bash_read_available_note(self):
        v = self._render()
        # Read-only step: Bash/Read available for querying the index commands.
        assert "read-only step" in v.lower()


# ---------------------------------------------------------------------------
# Root-view injection helper
# ---------------------------------------------------------------------------

class TestBuildRootViewInjection:
    def test_returns_self_describing_section_with_specs(self, tmp_path):
        # Lay down a minimal spec so the root view has real content.
        spec_dir = tmp_path / "se3" / "specs" / "base"
        spec_dir.mkdir(parents=True)
        (spec_dir / "spec.md").write_text(
            "<!-- spec-format: v1 -->\n"
            "# base Specification\n\n"
            "## Purpose\n"
            "Project baseline conventions for the demo project.\n\n"
            "## Requirements\n\n"
            "### Requirement: Demo\n"
            "Demo requirement body paragraph.\n",
            encoding="utf-8",
        )
        out = _build_root_view_injection(tmp_path)
        assert out  # non-empty
        assert "Root Index View" in out
        assert "se3 spec index <spec>" in out
        assert "se3 spec show <spec>::<requirement>" in out
        assert "se3/cache/spec-index.json" in out

    def test_degrades_to_empty_on_failure(self):
        # An import failure inside the helper must yield "" rather than raise.
        with patch(
            "se3.engine.spec_index.load_or_build", side_effect=RuntimeError("boom")
        ):
            out = _build_root_view_injection(__import__("pathlib").Path("/tmp/x_none"))
        assert out == ""


# ---------------------------------------------------------------------------
# Handler-level: the assembled prompt carries protocol + root view
# ---------------------------------------------------------------------------

def _seed_specs(root):
    spec_dir = root / "se3" / "specs" / "base"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text(
        "<!-- spec-format: v1 -->\n"
        "# base Specification\n\n"
        "## Purpose\n"
        "Project baseline conventions for the demo project.\n\n"
        "## Requirements\n\n"
        "### Requirement: Demo\n"
        "Demo requirement body paragraph.\n",
        encoding="utf-8",
    )


class TestUpdateSpecHandlerInjection:
    @pytest.fixture
    def flow(self, tmp_path):
        # project_root = change_path.parent, so seed specs under tmp_path.
        flow = FlowInstance(
            flow_id="test-flow-g7",
            task_description="Add feature",
            task_type="feature",
            status=FlowStatus.RUNNING,
            change_path=tmp_path / "changes" / "test",
        )
        flow.state.selected_steps = [StepType.UPDATE_SPEC]
        _seed_specs(tmp_path)
        return flow

    def _make_step(self):
        return Step(
            step_type=StepType.UPDATE_SPEC,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "Add feature",
                "changes_made": {"files_changed": ["src/foo.py"]},
                "verification_result": {"verified": True, "summary": "OK"},
            },
        )

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.context_builder.get_step_language_instruction", return_value="")
    @patch("se3.engine.context_builder.get_runtime_environment_injection", return_value="")
    def test_handler_prompt_has_protocol_and_root_view(self, _re, _lang, _inj, flow):
        step = self._make_step()
        with patch("se3.engine.steps.update_spec.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = '{"specs_updated": [], "new_capabilities": []}'
            mock_cls.return_value = mock_caller

            result = update_spec_handler(step, flow)

            prompt = mock_caller.call.call_args[1]["prompt"]
            # Index-first protocol + directed Read+Edit
            assert "se3 spec index" in prompt
            assert "se3 spec show <spec>::<requirement>" in prompt
            assert "Directed edit" in prompt
            # Base admission standard
            assert "base Spec Admission Standard" in prompt
            # Root index view appended (replaces plain spec-names list)
            assert "Root Index View" in prompt
            # New-spec domain marker guidance
            assert "<!-- domain:" in prompt
            assert result == StepStatus.COMPLETED


class TestVerifySpecHandlerInjection:
    @pytest.fixture
    def flow(self, tmp_path):
        flow = FlowInstance(
            flow_id="test-flow-g7v",
            task_description="Add feature",
            task_type="feature",
            status=FlowStatus.RUNNING,
            change_path=tmp_path / "changes" / "test",
        )
        flow.state.selected_steps = [StepType.VERIFY_SPEC]
        _seed_specs(tmp_path)
        return flow

    def _make_step(self):
        return Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "Add feature",
                "spec_content": {"base": "..."},
                "changes_made": {"files_changed": ["src/foo.py"]},
                "test_results": {"tests_blocking": False},
            },
        )

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.context_builder.get_spec_names_injection", return_value="")
    @patch("se3.engine.context_builder.get_runtime_environment_injection", return_value="")
    def test_handler_prompt_has_index_protocol(self, _re, _names, _inj, flow):
        step = self._make_step()
        with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = (
                '{"issues": [], "summary": "ok", '
                '"test_analysis": {"tests_passed": true}}'
            )
            mock_cls.return_value = mock_caller

            result = verify_spec_handler(step, flow)

            prompt = mock_caller.call.call_args[1]["prompt"]
            assert "se3 spec index" in prompt
            assert "se3 spec show <spec>::<requirement>" in prompt
            assert "se3/cache/spec-index.json" in prompt
            assert result == StepStatus.COMPLETED
