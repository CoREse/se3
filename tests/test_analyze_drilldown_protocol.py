"""G6 tests: analyze's root-view + drill-down selection protocol.

Covers the protocol改造 added by group G6 (``src/se3/engine/steps/analyze.py``):

- The analyze prompt no longer injects the full item list; it injects only the
  ROOT VIEW (the same renderer as ``se3 spec index``) plus the drill-down
  protocol text, so the agent discovers items on demand within its single
  ``caller.call()``.
- The out-port validation (``_validate_selected_items_against_flat_set``) checks
  every ``selected_items`` entry against the flat item full set by its full
  ``<spec>::<requirement>`` address: a domain group name, a ``pN`` page handle,
  or any intermediate navigation node is rejected and fed back to the LLM to
  retry; a real leaf address (or ``requirement_name == "*"`` whole-spec select)
  passes.
- ``requirement_name == "*"`` semantics are preserved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from se3.engine import steps as _steps  # noqa: F401  (ensure package import)
from se3.engine.steps import analyze
from se3.engine.models import FlowInstance, FlowStatus, Step, StepStatus, StepType
from se3.engine.spec_format import SPEC_FORMAT_VERSION_MARKER
from se3.engine.spec_index import load_or_build
from se3.engine.spec_index_render import render_index


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _write_spec(project_root: Path, name: str, content: str) -> Path:
    spec_dir = project_root / "se3" / "specs" / name
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_file = spec_dir / "spec.md"
    spec_file.write_text(content, encoding="utf-8")
    return spec_file


def _simple_spec(title: str, domain: str | None, *reqs: tuple[str, str]) -> str:
    head = [SPEC_FORMAT_VERSION_MARKER]
    if domain is not None:
        head.append(f"<!-- domain: {domain} -->")
    head += [
        "",
        f"# {title} Specification",
        "",
        "## Purpose",
        "",
        f"{title} governs the example subsystem in one sentence.",
        "",
    ]
    body = []
    for rname, rbody in reqs:
        body += [f"### Requirement: {rname}", "", rbody, ""]
    return "\n".join(head + body)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A temp project with a base spec and one item-bearing spec (alpha)."""
    _write_spec(
        tmp_path,
        "base",
        _simple_spec(
            "base",
            None,
            ("Project Identity", "The project baseline summary sentence."),
        ),
    )
    _write_spec(
        tmp_path,
        "alpha",
        _simple_spec(
            "alpha",
            "engine/steps",
            ("First Req", "First requirement opening summary sentence."),
            ("Second Req", "Second requirement opening summary sentence."),
        ),
    )
    return tmp_path


def _make_flow(project_root: Path) -> FlowInstance:
    flow = FlowInstance(
        task_description="do something",
        task_type="feature",
        status=FlowStatus.INIT,
    )
    # project_root = flow.change_path.parent
    flow.change_path = project_root / "change"
    return flow


def _make_step() -> Step:
    return Step(
        step_type=StepType.ANALYZE,
        inputs={"task_description": "Improve the alpha subsystem"},
    )


def _resp(selected_items: list[dict], task_type: str = "feature") -> str:
    import json

    return json.dumps(
        {
            "task_type": task_type,
            "scope": "alpha",
            "complexity": "medium",
            "reasoning": "because",
            "selected_items": selected_items,
        }
    )


def _install_fake_caller(monkeypatch, responses: list[str]) -> dict:
    """Replace ``analyze.LLMCaller`` with a scripted fake; record the prompts."""
    state = {"prompts": [], "responses": list(responses)}

    class FakeCaller:
        def __init__(self, *args, **kwargs):
            pass

        def call(self, prompt, **kwargs):
            state["prompts"].append(prompt)
            return state["responses"].pop(0)

    monkeypatch.setattr(analyze, "LLMCaller", FakeCaller)
    return state


# ---------------------------------------------------------------------------
# Prompt content: root view + drill-down protocol, no full item injection
# ---------------------------------------------------------------------------

def test_prompt_template_has_no_full_item_injection():
    """The static template injects the root view, not the full item list."""
    assert "{root_view}" in analyze.ANALYZE_PROMPT
    assert "available_items" not in analyze.ANALYZE_PROMPT
    assert "## Available Items" not in analyze.ANALYZE_PROMPT
    # Drill-down protocol commands are documented in the template.
    assert "se3 spec index" in analyze.ANALYZE_PROMPT
    assert "se3 spec show" in analyze.ANALYZE_PROMPT
    assert "Drill-down protocol" in analyze.ANALYZE_PROMPT


def test_handler_injects_root_view_consistent_with_renderer(project, monkeypatch):
    """The prompt embeds exactly the ``render_index`` root view, and omits the
    individual Requirement items (those are drilled for, not injected)."""
    state = _install_fake_caller(
        monkeypatch,
        [_resp([{"spec": "alpha", "requirement_name": "First Req"}])],
    )

    flow = _make_flow(project)
    step = _make_step()
    status = analyze.analyze_handler(step, flow)
    assert status == StepStatus.COMPLETED

    first_prompt = state["prompts"][0]

    # Root view (same renderer as `se3 spec index`) is embedded verbatim.
    index = load_or_build(project)
    root_view = render_index(index, spec=None)
    assert root_view in first_prompt

    # The full item index is NOT injected: the requirement names do not appear
    # in the initial prompt (the agent must drill `se3 spec index alpha`).
    assert "First Req" not in first_prompt
    assert "Second Req" not in first_prompt
    # But the spec name + drill command DO appear via the root view.
    assert "se3 spec index alpha" in first_prompt
    assert "se3 spec show" in first_prompt


# ---------------------------------------------------------------------------
# Out-port validation: flat-set membership
# ---------------------------------------------------------------------------

def test_validate_accepts_real_leaf_addresses(project):
    index = load_or_build(project)
    invalid = analyze._validate_selected_items_against_flat_set(
        [
            {"spec": "alpha", "requirement_name": "First Req"},
            {"spec": "alpha", "requirement_name": "Second Req"},
        ],
        index,
    )
    assert invalid == []


def test_validate_accepts_wildcard_for_existing_spec(project):
    """``requirement_name == "*"`` is preserved as valid for a known spec."""
    index = load_or_build(project)
    invalid = analyze._validate_selected_items_against_flat_set(
        [{"spec": "alpha", "requirement_name": "*"}],
        index,
    )
    assert invalid == []


def test_validate_rejects_group_and_page_handles(project):
    """A domain group name / page handle is not a selectable item."""
    index = load_or_build(project)
    invalid = analyze._validate_selected_items_against_flat_set(
        [
            {"spec": "alpha", "requirement_name": "engine/steps"},  # domain group
            {"spec": "alpha", "requirement_name": "p1"},            # page handle
        ],
        index,
    )
    assert "alpha::engine/steps" in invalid
    assert "alpha::p1" in invalid


def test_validate_rejects_intermediate_node_and_unknowns(project):
    index = load_or_build(project)
    invalid = analyze._validate_selected_items_against_flat_set(
        [
            {"spec": "alpha", "requirement_name": "alpha"},        # spec-name as req
            {"spec": "ghost", "requirement_name": "*"},            # unknown spec wildcard
            {"spec": "alpha", "requirement_name": "Nonexistent"},  # unknown requirement
        ],
        index,
    )
    assert "alpha::alpha" in invalid
    assert "ghost::*" in invalid
    assert "alpha::Nonexistent" in invalid


def test_keep_valid_items_drops_invalid(project):
    index = load_or_build(project)
    kept = analyze._keep_valid_items(
        [
            {"spec": "alpha", "requirement_name": "First Req"},   # valid
            {"spec": "alpha", "requirement_name": "p1"},          # handle -> dropped
            {"spec": "alpha", "requirement_name": "*"},           # wildcard -> kept
        ],
        index,
    )
    assert {"spec": "alpha", "requirement_name": "First Req"} in kept
    assert {"spec": "alpha", "requirement_name": "*"} in kept
    assert {"spec": "alpha", "requirement_name": "p1"} not in kept


def test_validation_feedback_lists_addresses_and_drill_hint():
    fb = analyze._build_validation_feedback(["alpha::engine/steps", "alpha::p1"])
    assert "Selection Validation Error" in fb
    assert "alpha::engine/steps" in fb
    assert "alpha::p1" in fb
    assert "se3 spec index" in fb


# ---------------------------------------------------------------------------
# Handler-level retry behaviour
# ---------------------------------------------------------------------------

def test_handler_reprompts_on_invalid_then_succeeds(project, monkeypatch):
    """A group-name selection triggers a re-prompt carrying the feedback, and a
    subsequent valid selection completes the step."""
    state = _install_fake_caller(
        monkeypatch,
        [
            # 1st: an invalid group-name selection.
            _resp([{"spec": "alpha", "requirement_name": "engine/steps"}]),
            # 2nd: a valid leaf selection.
            _resp([{"spec": "alpha", "requirement_name": "First Req"}]),
        ],
    )

    flow = _make_flow(project)
    step = _make_step()
    status = analyze.analyze_handler(step, flow)
    assert status == StepStatus.COMPLETED

    # Exactly two calls were made (one re-prompt).
    assert len(state["prompts"]) == 2
    # The second prompt carries the validation feedback (re-prompt channel).
    assert "Selection Validation Error" in state["prompts"][1]
    assert "alpha::engine/steps" in state["prompts"][1]
    # The first prompt did NOT carry feedback.
    assert "Selection Validation Error" not in state["prompts"][0]

    # The final selection is the valid leaf only.
    assert step.outputs["selected_items"] == [
        {"spec": "alpha", "requirement_name": "First Req"}
    ]


def test_handler_valid_selection_no_retry(project, monkeypatch):
    """A first-shot valid leaf selection makes exactly one call."""
    state = _install_fake_caller(
        monkeypatch,
        [_resp([{"spec": "alpha", "requirement_name": "Second Req"}])],
    )

    flow = _make_flow(project)
    step = _make_step()
    status = analyze.analyze_handler(step, flow)
    assert status == StepStatus.COMPLETED
    assert len(state["prompts"]) == 1
    assert step.outputs["selected_items"] == [
        {"spec": "alpha", "requirement_name": "Second Req"}
    ]


def test_handler_wildcard_selection_no_retry(project, monkeypatch):
    """``requirement_name == "*"`` is accepted without a re-prompt (semantics
    preserved)."""
    state = _install_fake_caller(
        monkeypatch,
        [_resp([{"spec": "alpha", "requirement_name": "*"}])],
    )

    flow = _make_flow(project)
    step = _make_step()
    status = analyze.analyze_handler(step, flow)
    assert status == StepStatus.COMPLETED
    assert len(state["prompts"]) == 1
    assert step.outputs["selected_items"] == [
        {"spec": "alpha", "requirement_name": "*"}
    ]


def test_handler_exhausts_retries_then_drops_invalid(project, monkeypatch):
    """After the bounded retries the invalid entries are dropped; a valid leaf
    in the same selection survives and the step still completes."""
    bad_and_good = _resp(
        [
            {"spec": "alpha", "requirement_name": "engine/steps"},  # invalid
            {"spec": "alpha", "requirement_name": "First Req"},     # valid
        ]
    )
    # Always invalid -> exhausts retries; valid entry survives the final drop.
    state = _install_fake_caller(
        monkeypatch,
        [bad_and_good, bad_and_good, bad_and_good],
    )

    flow = _make_flow(project)
    step = _make_step()
    status = analyze.analyze_handler(step, flow)
    assert status == StepStatus.COMPLETED
    # MAX retries (2) + initial = 3 calls.
    assert len(state["prompts"]) == 3
    # Only the valid leaf survives.
    assert step.outputs["selected_items"] == [
        {"spec": "alpha", "requirement_name": "First Req"}
    ]


def test_handler_no_llm_call_in_pure_validation(project):
    """The validation / render path performs no LLM call: the renderer and
    validators are pure over the index."""
    index = load_or_build(project)
    # Render and validate without ever constructing an LLMCaller.
    root_view = render_index(index, spec=None)
    assert "se3 spec index" in root_view
    invalid = analyze._validate_selected_items_against_flat_set(
        [{"spec": "alpha", "requirement_name": "First Req"}], index
    )
    assert invalid == []
