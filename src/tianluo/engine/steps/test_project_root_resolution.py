"""Tests for the shared flow project_root resolver and the discovery round-1
write-side fix.

Colocated engine test (allowed by the charter's testing convention). Covers:

1. ``resolve_flow_project_root`` three-level priority
   (context['project_root'] → change_path.parent → Path.cwd()), including the
   bare-flow (``flow.state is None``) fallback.
2. Regression for the WebUI "first user message + first reply invisible" bug:
   with a worktree-mode flow whose authoritative project_root lives in
   ``context['project_root']`` while the process cwd sits elsewhere (simulating
   the wrapper cwd pinned to the main checkout), discovery round-1's chat
   history must be written under the WORKTREE root, not the cwd. Plus the
   compatibility branch: with no context value, it falls back to the cwd.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType
from tianluo.engine.steps._project_root import resolve_flow_project_root


# --- unit: resolve_flow_project_root priority -------------------------------


def test_context_project_root_wins_over_cwd_and_change_path(tmp_path, monkeypatch):
    """context['project_root'] is authoritative: cwd and change_path ignored."""
    worktree = tmp_path / "worktree"
    main = tmp_path / "main"
    worktree.mkdir()
    main.mkdir()
    monkeypatch.chdir(main)

    flow = FlowInstance()
    flow.state.context["project_root"] = str(worktree)
    # Even a present change_path must not override the authoritative context.
    flow.change_path = main / "change.json"

    assert resolve_flow_project_root(flow) == worktree


def test_change_path_used_when_context_missing(tmp_path, monkeypatch):
    """No context value → fall back to change_path.parent."""
    monkeypatch.chdir(tmp_path)
    flow = FlowInstance()
    # No context['project_root'] set.
    flow.change_path = tmp_path / "sub" / "change.json"

    assert resolve_flow_project_root(flow) == tmp_path / "sub"


def test_cwd_used_when_context_and_change_path_missing(tmp_path, monkeypatch):
    """Neither context nor change_path → Path.cwd()."""
    monkeypatch.chdir(tmp_path)
    flow = FlowInstance()  # no context value, change_path is None

    assert resolve_flow_project_root(flow) == Path(os.getcwd())


def test_bare_flow_with_no_state_falls_back_to_cwd(tmp_path, monkeypatch):
    """A bare flow (state is None) must not raise; it falls back to cwd."""
    monkeypatch.chdir(tmp_path)
    flow = FlowInstance()
    flow.state = None  # bare test-constructed flow

    assert resolve_flow_project_root(flow) == Path(os.getcwd())


def test_empty_context_value_falls_through(tmp_path, monkeypatch):
    """An empty-string context value is treated as absent (falls through)."""
    monkeypatch.chdir(tmp_path)
    flow = FlowInstance()
    flow.state.context["project_root"] = ""
    flow.change_path = tmp_path / "cp" / "change.json"

    assert resolve_flow_project_root(flow) == tmp_path / "cp"


# --- integration: discovery round-1 write-side lands in the worktree --------


class _FakeLLMCaller:
    """Stand-in for LLMCaller that faithfully writes chat history to the
    project_root it is constructed with (as the real caller does), then
    returns a valid discovery JSON response — no real subprocess / LLM.
    """

    def __init__(self, project_root, flow_id, step_id, step_type, **kwargs):
        self.project_root = Path(project_root)
        self.flow_id = flow_id
        self.step_id = step_id
        self.step_type = step_type
        self.last_raw_result = ""

    def call(self, prompt, **kwargs):
        # Mirror the real caller's write side: the user prompt and the
        # assistant response both land under self.project_root/se3/history.
        from tianluo.engine import chat_history

        chat_history.record_prompt(
            self.project_root,
            self.flow_id,
            self.step_id,
            self.step_type,
            prompt,
            attempt=0,
        )
        chat_history.record_response(
            self.project_root,
            self.flow_id,
            self.step_id,
            self.step_type,
            raw_ndjson="",
            attempt=0,
        )
        response = (
            '{"mode": "question", "content": "What is the scope?", '
            '"questions": ["scope?"], "refined_description": ""}'
        )
        self.last_raw_result = response
        return response


def _make_discovery_flow(project_root_value):
    flow = FlowInstance(task_description="do the thing")
    if project_root_value is not None:
        flow.state.context["project_root"] = str(project_root_value)
    # Modern flows carry change_path = None — this is exactly the shape that
    # triggered the bug when the resolver fell back to Path.cwd().
    flow.change_path = None
    step = Step(step_type=StepType.DISCOVERY, step_id="discovery")
    step.inputs["task_description"] = flow.task_description
    step.inputs["discovery_state"] = {"round": 0, "history": []}
    flow.state.steps[step.step_id] = step
    return flow, step


def test_discovery_round1_history_lands_in_worktree_not_cwd(tmp_path, monkeypatch):
    """The core regression: worktree flow round-1 history must be written to the
    worktree root even though the process cwd is the main checkout."""
    from tianluo.engine.steps import discovery

    worktree_root = tmp_path / "worktree"
    main_cwd = tmp_path / "main"
    worktree_root.mkdir()
    main_cwd.mkdir()
    # Simulate the wrapper process: cwd pinned to the main checkout.
    monkeypatch.chdir(main_cwd)
    monkeypatch.setattr(discovery, "LLMCaller", _FakeLLMCaller)

    flow, step = _make_discovery_flow(worktree_root)

    status = discovery.discovery_handler(step, flow)
    assert status == StepStatus.PAUSED  # question mode pauses for the user

    worktree_hist = worktree_root / "se3" / "history" / flow.flow_id
    main_hist = main_cwd / "se3" / "history" / flow.flow_id
    assert worktree_hist.is_dir(), "round-1 history must land under the worktree"
    assert (worktree_hist / "discovery.jsonl").exists()
    assert not main_hist.exists(), "no history may fork into the main checkout"


def test_discovery_round1_falls_back_to_cwd_without_context(tmp_path, monkeypatch):
    """Compatibility branch: no context['project_root'] and no change_path →
    the resolver (and thus the write side) falls back to the process cwd."""
    from tianluo.engine.steps import discovery

    main_cwd = tmp_path / "main"
    main_cwd.mkdir()
    monkeypatch.chdir(main_cwd)
    monkeypatch.setattr(discovery, "LLMCaller", _FakeLLMCaller)

    flow, step = _make_discovery_flow(None)  # no authoritative context value

    status = discovery.discovery_handler(step, flow)
    assert status == StepStatus.PAUSED

    cwd_hist = main_cwd / "se3" / "history" / flow.flow_id
    assert cwd_hist.is_dir(), "without context, history falls back to cwd"
    assert (cwd_hist / "discovery.jsonl").exists()
