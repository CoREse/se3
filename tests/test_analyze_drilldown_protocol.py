"""G6 tests: analyze's root-view + drill-down selection protocol.

Covers the protocol改造 added by group G6 (``src/se3/engine/steps/analyze.py``):

- The analyze prompt no longer injects the full item list; it injects only the
  ROOT VIEW (the same renderer as ``se3 spec index``) plus the drill-down
  protocol text, so the agent discovers items on demand within its single
  ``caller.call()``.
- The out-port validation (``_validate_selected_items_against_flat_set``) checks
  every ``selected_items`` entry against the flat item full set by its full
  ``<spec>::<requirement>`` address: a domain group name, a ``pN`` page handle,
  or any intermediate navigation node is rejected; a real leaf address (or
  ``requirement_name == "*"`` whole-spec select) passes.
- A non-item address surfacing in the result (group/page handle, intermediate
  node) is treated as a validation failure. Each attempt is one ``caller.call()``
  (the subprocess carries its own internal tool loop for drilling down). On
  validation failure — whether the selection is entirely group/page handles or a
  mix of valid leaves and invalid handles (ANY invalid address fails the whole
  selection; a valid sibling never suppresses it) — the handler feeds the
  rejected addresses back into a fresh selection call and auto-retries in-step,
  up to ``MAX_SELECTION_ATTEMPTS``, WITHOUT pausing for a human Retry/Skip/Abort
  decision. Only after exhausting those attempts does the step return FAILED with
  the rejected addresses in ``error_message`` (and record the diagnosis into the
  step's chat history) so the engine-level retry replays it as feedback, rather
  than substituting a ``base::*`` fallback that would mask the failure.
  ``base::*`` remains the fallback only for a genuinely empty (no-handle)
  selection.
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
    """Replace ``analyze.LLMCaller`` with a scripted fake; record the prompts.

    Responses are consumed in order; once the scripted list is exhausted the
    last response is repeated, so an exhaustion test can supply a single invalid
    response and have it returned for every in-step retry attempt.
    """
    state = {"prompts": [], "responses": list(responses), "last": None}

    class FakeCaller:
        def __init__(self, *args, **kwargs):
            pass

        def call(self, prompt, **kwargs):
            state["prompts"].append(prompt)
            if state["responses"]:
                state["last"] = state["responses"].pop(0)
            return state["last"]

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


# ---------------------------------------------------------------------------
# Handler-level out-port validation: bounded in-step auto-retry + engine retry
# ---------------------------------------------------------------------------

def test_handler_invalid_selection_auto_retries_then_succeeds(project, monkeypatch):
    """An invalid (group-name-only) first selection does NOT fail immediately:
    the handler feeds the rejected address back into a fresh selection call and
    retries automatically (no human intervention). A subsequent valid leaf
    selection completes the step."""
    invalid = _resp([{"spec": "alpha", "requirement_name": "engine/steps"}])
    good = _resp([{"spec": "alpha", "requirement_name": "First Req"}])
    state = _install_fake_caller(monkeypatch, [invalid, good])

    flow = _make_flow(project)
    step = _make_step()
    status = analyze.analyze_handler(step, flow)
    assert status == StepStatus.COMPLETED

    # Two calls: the rejected attempt, then the auto-retried successful one.
    assert len(state["prompts"]) == 2

    # The retry prompt fed the rejected non-item address back to the LLM.
    assert "Previous Selection Rejected" in state["prompts"][1]
    assert "alpha::engine/steps" in state["prompts"][1]

    # The valid leaf selection landed, not a base::* fallback.
    assert step.outputs["selected_items"] == [
        {"spec": "alpha", "requirement_name": "First Req"}
    ]


def test_handler_invalid_selection_exhausts_retries_then_fails(project, monkeypatch):
    """A persistently-invalid selection retries in-step up to
    ``MAX_SELECTION_ATTEMPTS`` and only then FAILs for the engine-level retry
    path. The failure surfaces the rejected addresses and never silently degrades
    to a base::* fallback."""
    invalid = _resp([{"spec": "alpha", "requirement_name": "engine/steps"}])
    state = _install_fake_caller(monkeypatch, [invalid])

    flow = _make_flow(project)
    step = _make_step()
    status = analyze.analyze_handler(step, flow)
    assert status == StepStatus.FAILED

    # The handler retried in-step up to the bound before deferring to the engine.
    assert len(state["prompts"]) == analyze.MAX_SELECTION_ATTEMPTS

    # The failure surfaces the rejected non-item address (so the engine retry can
    # feed it back); selected_items is NOT populated with a base::* fallback.
    assert step.error_message
    assert "alpha::engine/steps" in step.error_message
    assert step.outputs.get("selected_items") != [
        {"spec": "base", "requirement_name": "*"}
    ]


def test_handler_all_invalid_records_feedback_for_retry(project, monkeypatch):
    """When the in-step retries are exhausted, the post-validation diagnosis is
    recorded into the step's chat history as a user-role turn, so the
    engine-level retry's ``format_history_for_retry`` surfaces it (the validation
    runs AFTER ``caller.call()``, so it is otherwise absent from the recorded
    prompt/response history and a fresh engine-issued attempt would repeat the
    same invalid handle)."""
    _install_fake_caller(
        monkeypatch,
        [_resp([{"spec": "alpha", "requirement_name": "engine/steps"}])],
    )

    flow = _make_flow(project)
    step = _make_step()
    # The state machine populates step_id in real runs (the LLM caller keys its
    # own prompt/response history on it too); set it so the feedback record lands.
    step.step_id = "01_analyze_feedbacktest"
    status = analyze.analyze_handler(step, flow)
    assert status == StepStatus.FAILED

    # The diagnosis is persisted as a user-role chat record under the step's
    # history, tagged so the retry context replays it as feedback.
    from se3.engine.chat_history import format_history_for_retry, get_step_history

    session = get_step_history(project, flow.flow_id, step.step_id)
    assert session is not None
    user_feedback = [
        m for m in session.messages
        if m.role == "user" and "non-item address" in m.content
    ]
    assert user_feedback, "selection-failure diagnosis was not recorded for retry"
    assert "alpha::engine/steps" in user_feedback[0].content

    # And it actually appears in the retry context the next analyze call prepends.
    retry_ctx = format_history_for_retry(project, flow.flow_id, step.step_id)
    assert retry_ctx is not None
    assert "alpha::engine/steps" in retry_ctx


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


def test_handler_mixed_invalid_auto_retries(project, monkeypatch):
    """A mixed selection (one invalid handle + one valid leaf) is REJECTED — a
    group/page handle represents additional Requirements the LLM meant to select,
    so it MUST NOT be silently dropped while proceeding on the valid sibling (that
    would feed downstream steps incomplete spec context and mask the failure). The
    valid sibling does NOT suppress the rejection; the handler feeds the rejected
    handle back and auto-retries in-step rather than pausing. A subsequent valid
    selection completes the step."""
    bad_and_good = _resp(
        [
            {"spec": "alpha", "requirement_name": "engine/steps"},  # invalid handle
            {"spec": "alpha", "requirement_name": "First Req"},     # valid leaf
        ]
    )
    good = _resp(
        [
            {"spec": "alpha", "requirement_name": "First Req"},
            {"spec": "alpha", "requirement_name": "Second Req"},
        ]
    )
    state = _install_fake_caller(monkeypatch, [bad_and_good, good])

    flow = _make_flow(project)
    step = _make_step()
    status = analyze.analyze_handler(step, flow)
    assert status == StepStatus.COMPLETED
    # The mixed selection was rejected and auto-retried in-step.
    assert len(state["prompts"]) == 2
    # The retry prompt fed the rejected handle back to the LLM.
    assert "alpha::engine/steps" in state["prompts"][1]
    # The valid re-selection landed; the partial mixed selection never leaked.
    assert step.outputs["selected_items"] == [
        {"spec": "alpha", "requirement_name": "First Req"},
        {"spec": "alpha", "requirement_name": "Second Req"},
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
